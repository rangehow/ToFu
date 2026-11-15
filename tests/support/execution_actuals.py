"""Read a task's consumed tokens and completeness as its execution account reports them."""
from lib.cost import merge_usage_totals, observed_usage_totals


def account_actuals(task):
    """Return ``(tokens, complete)``: the root account wins over per-task usage."""
    usage = merge_usage_totals(task.get('usage'), task.get('_swarmChildUsage'))
    tokens = max(observed_usage_totals(usage)[0], int(task.get('_executionModelActualTokens') or 0))
    complete = task.get('_executionActualsComplete') is not False
    budget = task.get('_executionBudget')
    if budget is not None and budget.root_id == task.get('id'):
        tokens, _, account_complete = budget.consumption_snapshot()
        complete = complete and account_complete
    return tokens, complete


def account_tokens(task):
    return account_actuals(task)[0]
