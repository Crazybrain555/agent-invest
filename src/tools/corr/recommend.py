from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd


def recommend_by_cluster(
    corr_matrix: pd.DataFrame, threshold: float
) -> tuple[List[str], List[dict]]:
    if corr_matrix.empty:
        return [], []

    abs_matrix = corr_matrix.abs().copy()
    np.fill_diagonal(abs_matrix.values, 0.0)
    factors = list(abs_matrix.index)
    abs_values = abs_matrix.values
    adjacency = abs_values >= threshold

    visited: set[int] = set()
    recommendations: List[str] = []
    clusters: List[dict] = []

    for idx in range(len(factors)):
        if idx in visited:
            continue
        stack = [idx]
        visited.add(idx)
        component: List[int] = []
        while stack:
            current = stack.pop()
            component.append(current)
            neighbors = np.where(adjacency[current])[0].tolist()
            for neighbor in neighbors:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)

        members = [factors[i] for i in component]
        if len(members) == 1:
            recommendations.append(members[0])
            continue

        comp_vals = abs_values[np.ix_(component, component)].astype(float)
        np.fill_diagonal(comp_vals, np.nan)
        avg_abs = np.nanmean(comp_vals, axis=1)
        if np.all(np.isnan(avg_abs)):
            chosen_idx = sorted(component, key=lambda i: factors[i])[0]
            avg_value = float("nan")
        else:
            min_value = np.nanmin(avg_abs)
            candidates = [
                component[i] for i, value in enumerate(avg_abs) if np.isclose(value, min_value, equal_nan=False)
            ]
            chosen_idx = sorted(candidates, key=lambda i: factors[i])[0]
            avg_value = float(avg_abs[component.index(chosen_idx)])

        chosen = factors[chosen_idx]
        recommendations.append(chosen)
        clusters.append(
            {
                "members": sorted(members),
                "selected": chosen,
                "avg_abs_corr_in_cluster": avg_value,
            }
        )

    recommendations = sorted(set(recommendations))
    clusters = sorted(clusters, key=lambda item: item["selected"])
    return recommendations, clusters
