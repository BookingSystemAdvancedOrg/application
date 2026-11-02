"""Margin per dish (feature J, dishMargin): adds cost, margin and marginPct
to the insights dishes and missingCost, targetMarginPct to the summary,
from the costs the dish-margin feature answers (insights.dishCosts hook:
{targetMarginPct, costs: {menuItemId: [{validFrom, costOre}]}}).

Run 0 stub: returns the summary unchanged. Feature J owns this file.
"""


def apply(summary, costs):
    return summary
