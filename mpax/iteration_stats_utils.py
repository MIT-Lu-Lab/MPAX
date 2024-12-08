from typing import NamedTuple, Tuple

import jax
import jax.numpy as jnp

from mpax.solver_log import log_iteration_stats
from mpax.utils import (
    CachedQuadraticProgramInfo,
    ConvergenceInformation,
    InfeasibilityInformation,
    IterationStats,
    PdhgSolverState,
    PointType,
    QuadraticProgrammingProblem,
    ScaledQpProblem,
)


def compute_primal_residual_constraint(
    activities: jnp.ndarray, right_hand_side: jnp.ndarray, equalities_mask: jnp.ndarray
) -> jnp.ndarray:
    """
    Kernel to compute the violation of primal constraints.

    Parameters
    ----------
    activities : jnp.ndarray
        Vector of activities.
    right_hand_side : jnp.ndarray
        Right-hand side vector.
    equalities_mask : jnp.ndarray
        Boolean array indicating equality constraints.

    Returns
    -------
    jnp.ndarray
        Constraint violation vector.
    """
    constraint_violation = jax.lax.select(
        equalities_mask,
        right_hand_side - activities,
        jnp.maximum(right_hand_side - activities, 0.0),
    )
    return constraint_violation


def compute_dual_objective(
    variable_lower_bound: jnp.ndarray,
    variable_upper_bound: jnp.ndarray,
    reduced_costs: jnp.ndarray,
    right_hand_side: jnp.ndarray,
    dual_solution: jnp.ndarray,
    objective_constant: float,
):
    """Compute the dual objective.

    Parameters
    ----------
    variable_lower_bound : jnp.ndarray
        the lower bound of variables
    variable_upper_bound : jnp.ndarray
        the upper bound of variables
    reduced_costs : jnp.ndarray
        the reduced costs
    right_hand_side : jnp.ndarray
        the right hand side of the constraints
    dual_solution : jnp.ndarray
        the dual solution
    objective_constant : float
        the constant term in the objective

    Returns
    -------
    float
        the dual objective
    """
    dual_objective_contribution_sum = jnp.sum(
        jnp.where(
            reduced_costs > 0.0,
            variable_lower_bound * reduced_costs,
            jnp.where(
                reduced_costs < 0.0,
                variable_upper_bound * reduced_costs,
                0.0,  # Handle the case where reduced_costs == 0
            ),
        )
    )
    base_dual_objective = jnp.dot(right_hand_side, dual_solution) + objective_constant
    return base_dual_objective + dual_objective_contribution_sum


def compute_reduced_costs_from_primal_gradient(
    primal_gradient: jnp.ndarray,
    isfinite_variable_lower_bound: jnp.ndarray,
    isfinite_variable_upper_bound: jnp.ndarray,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """
    Kernel to compute the reduced costs from the primal gradient.

    Parameters
    ----------
    primal_gradient : jnp.ndarray
        Primal gradient vector.
    isfinite_variable_lower_bound : jnp.ndarray
        Boolean array indicating finite lower bounds.
    isfinite_variable_upper_bound : jnp.ndarray
        Boolean array indicating finite upper bounds.

    Returns
    -------
    Tuple[jnp.ndarray, jnp.ndarray]
        Reduced costs and reduced costs violation vectors.
    """
    reduced_costs = (
        jnp.maximum(primal_gradient, 0.0) * isfinite_variable_lower_bound
        + jnp.minimum(primal_gradient, 0.0) * isfinite_variable_upper_bound
    )
    reduced_costs_violation = primal_gradient - reduced_costs
    return reduced_costs, reduced_costs_violation


# Note: the order of the calculations can be improved.
def compute_convergence_information(
    problem: QuadraticProgrammingProblem,
    qp_cache: CachedQuadraticProgramInfo,
    primal_iterate: jnp.ndarray,
    dual_iterate: jnp.ndarray,
    dual_residual: jnp.ndarray,
    eps_ratio: float,
    primal_product: jnp.ndarray,
    dual_product: jnp.ndarray,
) -> ConvergenceInformation:
    """
    Compute convergence information of the given primal and dual solutions.

    Relative versions of the residuals are defined as
      relative_residual = residual / (eps_ratio + norm),
    where
      eps_ratio = eps_abs / eps_rel
      residual = one of the residuals (l{2,_inf}_{primal,dual}_residual)
      norm = the relative norm (l{2,_inf} norm of
             {constraint_bounds,primal_linear_objective} respectively).

    1. If eps_rel = 0.0, these will all be 0.0.
    2. If eps_rel > 0.0, the absolute and relative termination
    criteria translate to relative_residual <= eps_rel.

    NOTE: The usefulness of these relative residuals is based on their
    relationship to TerminationCriteria. If the TerminationCriteria change
    consider adding additional iteration measures here.


    Parameters
    ----------
    problem : QuadraticProgrammingProblem
        Quadratic programming problem instance.
    qp_cache : CachedQuadraticProgramInfo
        Cached quadratic program information.
    primal_iterate : jnp.ndarray
        Primal iterate vector.
    dual_iterate : jnp.ndarray
        Dual iterate vector.
    eps_ratio : float
        Epsilon ratio for relative measures.
    primal_product : jnp.ndarray
        Primal product vector.
    dual_product : jnp.ndarray
        Dual product vector.

    Returns
    -------
    ConvergenceInformation
        Computed convergence information.
    """

    lower_variable_violation = jnp.maximum(
        problem.variable_lower_bound - primal_iterate, 0.0
    )
    upper_variable_violation = jnp.maximum(
        primal_iterate - problem.variable_upper_bound, 0.0
    )

    constraint_violation = jax.lax.select(
        problem.equalities_mask,
        problem.right_hand_side - primal_product,
        jnp.maximum(problem.right_hand_side - primal_product, 0.0),
    )

    primal_objective = problem.objective_constant + jnp.dot(
        problem.objective_vector, primal_iterate
    )

    violation_info = jnp.concatenate(
        [constraint_violation, lower_variable_violation, upper_variable_violation]
    )
    l_inf_primal_residual = jnp.linalg.norm(violation_info, ord=jnp.inf)
    l2_primal_residual = jnp.linalg.norm(violation_info, ord=2)

    l_inf_primal_variable = jnp.linalg.norm(primal_iterate, ord=jnp.inf)
    l2_primal_variable = jnp.linalg.norm(primal_iterate, ord=2)

    reduced_costs, reduced_costs_violation = compute_reduced_costs_from_primal_gradient(
        problem.objective_vector - dual_product,
        problem.isfinite_variable_lower_bound,
        problem.isfinite_variable_upper_bound,
    )

    dual_objective = compute_dual_objective(
        problem.variable_lower_bound,
        problem.variable_upper_bound,
        reduced_costs,
        problem.right_hand_side,
        dual_iterate,
        problem.objective_constant,
    )
    l_inf_dual_residual = jnp.linalg.norm(
        jnp.concatenate([dual_residual, reduced_costs_violation]), ord=jnp.inf
    )
    l2_dual_residual = jnp.linalg.norm(
        jnp.concatenate([dual_residual, reduced_costs_violation]), ord=2
    )
    l_inf_dual_variable = jnp.linalg.norm(dual_iterate, ord=jnp.inf)
    l2_dual_variable = jnp.linalg.norm(dual_iterate, ord=2)

    # Compute the relative information.
    relative_l_inf_primal_residual = l_inf_primal_residual / (
        eps_ratio + qp_cache.l_inf_norm_primal_right_hand_side
    )
    relative_l2_primal_residual = l2_primal_residual / (
        eps_ratio + qp_cache.l2_norm_primal_right_hand_side
    )
    relative_l_inf_dual_residual = l_inf_dual_residual / (
        eps_ratio + qp_cache.l_inf_norm_primal_linear_objective
    )
    relative_l2_dual_residual = l2_dual_residual / (
        eps_ratio + qp_cache.l2_norm_primal_linear_objective
    )
    corrected_dual_obj_value = jax.lax.cond(
        l_inf_dual_residual == 0.0,
        lambda _: dual_objective,
        lambda _: -jnp.inf,
        operand=None,
    )
    gap = jnp.abs(primal_objective - dual_objective)
    abs_obj = jnp.abs(primal_objective) + jnp.abs(dual_objective)
    relative_optimality_gap = gap / (eps_ratio + abs_obj)

    return ConvergenceInformation(
        PointType.POINT_TYPE_AVERAGE_ITERATE,
        primal_objective,
        dual_objective,
        corrected_dual_obj_value,
        l_inf_primal_residual,
        l2_primal_residual,
        l_inf_dual_residual,
        l2_dual_residual,
        relative_l_inf_primal_residual,
        relative_l2_primal_residual,
        relative_l_inf_dual_residual,
        relative_l2_dual_residual,
        relative_optimality_gap,
        l_inf_primal_variable,
        l2_primal_variable,
        l_inf_dual_variable,
        l2_dual_variable,
    )


def compute_infeasibility_information(
    problem: QuadraticProgrammingProblem,
    primal_ray_estimate: jnp.ndarray,
    dual_ray_estimate: jnp.ndarray,
    dual_residual: jnp.ndarray,
    primal_ray_estimate_product: jnp.ndarray,
    dual_ray_estimate_product: jnp.ndarray,
):
    """
    Compute infeasibility information of the given primal and dual solutions.

    Parameters
    ----------
    problem : QuadraticProgrammingProblem
        Quadratic programming problem instance.
    primal_ray_estimate : jnp.ndarray
        Primal ray estimate vector.
    dual_ray_estimate : jnp.ndarray
        Dual ray estimate vector.
    primal_ray_estimate_product : jnp.ndarray
        Primal ray estimate product vector.
    dual_ray_estimate_product : jnp.ndarray
        Dual ray estimate product vector.

    Returns
    -------
    InfeasibilityInformation
        Computed infeasibility information.
    """
    # Assume InfeasibilityInformation is a namedtuple
    primal_ray_inf_norm = jnp.linalg.norm(primal_ray_estimate, ord=jnp.inf)
    scaled_primal_ray_estimate, scaled_primal_ray_estimate_product = jax.lax.cond(
        primal_ray_inf_norm == 0.0,
        lambda _: (primal_ray_estimate, primal_ray_estimate_product),
        lambda _: (
            primal_ray_estimate / primal_ray_inf_norm,
            primal_ray_estimate_product / primal_ray_inf_norm,
        ),
        operand=None,
    )

    lower_variable_violation = jnp.maximum(
        (-1 / problem.isfinite_variable_lower_bound + 1) - scaled_primal_ray_estimate,
        0.0,
    )
    upper_variable_violation = jnp.maximum(
        scaled_primal_ray_estimate - (1 / problem.isfinite_variable_upper_bound - 1),
        0.0,
    )

    constraint_violation = jax.lax.select(
        problem.equalities_mask,
        jnp.zeros_like(problem.right_hand_side) - scaled_primal_ray_estimate_product,
        jnp.maximum(
            jnp.zeros_like(problem.right_hand_side)
            - scaled_primal_ray_estimate_product,
            0.0,
        ),
    )

    primal_objective = problem.objective_constant + jnp.dot(
        problem.objective_vector, scaled_primal_ray_estimate
    )

    max_primal_ray_infeasibility = jnp.linalg.norm(
        jnp.concatenate(
            [constraint_violation, lower_variable_violation, upper_variable_violation]
        ),
        ord=jnp.inf,
    )
    # Question: do we need to add objective_constant here?
    # Answer: No, we only need the direction here.
    primal_ray_linear_objective = jnp.dot(
        problem.objective_vector, scaled_primal_ray_estimate
    )
    reduced_costs, reduced_costs_violation = compute_reduced_costs_from_primal_gradient(
        -dual_ray_estimate_product,
        problem.isfinite_variable_lower_bound,
        problem.isfinite_variable_upper_bound,
    )
    dual_objective = compute_dual_objective(
        problem.variable_lower_bound,
        problem.variable_upper_bound,
        reduced_costs,
        problem.right_hand_side,
        dual_ray_estimate,
        problem.objective_constant,
    )

    l_inf_dual_residual = jnp.linalg.norm(
        jnp.concatenate([dual_residual, reduced_costs_violation]), ord=jnp.inf
    )

    l2_dual_residual = jnp.linalg.norm(
        jnp.concatenate([dual_residual, reduced_costs_violation]), ord=2
    )

    scaling_factor = jax.lax.max(
        jnp.linalg.norm(scaled_primal_ray_estimate, ord=jnp.inf),
        jnp.linalg.norm(reduced_costs, ord=jnp.inf),
    )
    max_dual_ray_infeasibility, dual_ray_objective = jax.lax.cond(
        scaling_factor == 0.0,
        lambda _: (0.0, 0.0),
        lambda _: (
            l_inf_dual_residual / scaling_factor,
            dual_objective / scaling_factor,
        ),
        operand=None,
    )

    return InfeasibilityInformation(
        PointType.POINT_TYPE_AVERAGE_ITERATE,
        max_primal_ray_infeasibility,
        primal_ray_linear_objective,
        max_dual_ray_infeasibility,
        dual_ray_objective,
    )


def evaluate_unscaled_iteration_stats(
    scaled_problem: ScaledQpProblem,
    qp_cache: CachedQuadraticProgramInfo,
    solver_state: PdhgSolverState,
    cumulative_time: float,
    eps_ratio: float,
    display_frequency: int,
    average: bool = True,
):
    """
    Compute the iteration stats of the unscaled primal and dual solutions.

    Parameters
    ----------
    scaled_problem : ScaledQpProblem
        Scaled quadratic programming problem instance.
    qp_cache : CachedQuadraticProgramInfo
        Cached quadratic program information.
    solver_state : PdhgSolverState
        The current solver state.
    cumulative_time : float
        Cumulative time in seconds.
    eps_ratio : float
        eps_abs / eps_rel
    display_frequency : int
        Frequency to display the iteration stats.
    average : bool
        Whether to use the average solution.

    Returns
    -------
    IterationStats
        Computed iteration statistics for the unscaled problem.
    """
    (
        unscaled_primal_solution,
        unscaled_dual_solution,
        unscaled_primal_product,
        unscaled_dual_product,
    ) = jax.lax.cond(
        average == True,
        lambda: (
            solver_state.avg_primal_solution / scaled_problem.variable_rescaling,
            solver_state.avg_dual_solution / scaled_problem.constraint_rescaling,
            solver_state.avg_primal_product * scaled_problem.constraint_rescaling,
            solver_state.avg_dual_product * scaled_problem.variable_rescaling,
        ),
        lambda: (
            solver_state.current_primal_solution / scaled_problem.variable_rescaling,
            solver_state.current_dual_solution / scaled_problem.constraint_rescaling,
            solver_state.current_primal_product * scaled_problem.constraint_rescaling,
            solver_state.current_dual_product * scaled_problem.variable_rescaling,
        ),
    )
    unscaled_dual_residual = jnp.where(
        scaled_problem.original_qp.inequalities_mask,
        jnp.maximum(-unscaled_dual_solution, 0.0),
        0.0,
    )
    convergence_information = compute_convergence_information(
        scaled_problem.original_qp,
        qp_cache,
        unscaled_primal_solution,
        unscaled_dual_solution,
        unscaled_dual_residual,
        eps_ratio,
        unscaled_primal_product,
        unscaled_dual_product,
    )
    infeasibility_information = compute_infeasibility_information(
        scaled_problem.original_qp,
        unscaled_primal_solution,
        unscaled_dual_solution,
        unscaled_dual_residual,
        unscaled_primal_product,
        unscaled_dual_product,
    )
    current_iteration_stats = IterationStats(
        iteration_number=solver_state.num_iterations,
        convergence_information=convergence_information,
        infeasibility_information=infeasibility_information,
        cumulative_kkt_matrix_passes=solver_state.cumulative_kkt_passes,
        cumulative_rejected_steps=0,  # cumulative_rejected_steps
        cumulative_time_sec=cumulative_time,
        step_size=solver_state.step_size,
        primal_weight=solver_state.primal_weight,
        method_specific_stats={},
    )
    log_iteration_stats(current_iteration_stats, solver_state, display_frequency)
    return current_iteration_stats


def should_log_iteration_status(iteration: int, params: NamedTuple) -> bool:
    """
    Determine if the iteration statistics should be printed based on the
    termination status, current iteration number, and display frequency.

    Parameters
    ----------
    iteration : int
        Current iteration number.
    params : NamedTuple
        Parameters for the solver.

    Returns
    -------
    bool
        Whether to print the iteration stats.
    """
    num_of_evaluations = (iteration - 1) // params.termination_evaluation_frequency
    # Print stats every display_frequency * termination_evaluation_frequency iterations
    return num_of_evaluations % params.display_frequency == 0