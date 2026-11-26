"""Q2. Fetch student test scores from an API, compute averages, draw a bar chart.

There is no public "student scores" API, so the default source is the local mock
(start it with `python mock_api.py`). Any API returning
[{id, name, scores: {subject: score_or_null}}] works.

    python q2_scores.py                                    # -> scores.png
    python q2_scores.py --url http://host/students --out chart.png
"""
import argparse
from statistics import mean

import matplotlib
matplotlib.use("Agg")  # render to a file, no display needed
import matplotlib.pyplot as plt

from q1_books import get_json


def averages(students):
    """Per-student average over the subjects they actually sat (null = absent, not zero),
    plus the class average over every individual score and per-subject averages."""
    per_student, all_scores, by_subject = [], [], {}
    for s in students:
        taken = {k: v for k, v in s.get("scores", {}).items() if isinstance(v, (int, float))}
        if not taken:
            continue
        per_student.append((s["name"], mean(taken.values())))
        all_scores.extend(taken.values())
        for subj, v in taken.items():
            by_subject.setdefault(subj, []).append(v)
    return (per_student, mean(all_scores) if all_scores else 0.0,
            {k: mean(v) for k, v in sorted(by_subject.items())})


def chart(per_student, class_avg, out):
    names = [n for n, _ in per_student]
    vals = [v for _, v in per_student]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = ["#2b6cb0" if v >= class_avg else "#a0aec0" for v in vals]
    bars = ax.bar(names, vals, color=colors)
    ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=9)
    ax.axhline(class_avg, color="#c05621", linestyle="--", linewidth=1.5,
               label=f"class average {class_avg:.1f}")
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.set_ylim(0, 112)
    ax.set_ylabel("Average score")
    ax.set_title("Average test score per student (blue = at or above class average)")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8765/students")
    p.add_argument("--out", default="scores.png")
    a = p.parse_args(argv)

    students = get_json(a.url)
    per_student, class_avg, by_subject = averages(students)
    for name, avg in sorted(per_student, key=lambda x: -x[1]):
        print(f"{name:<10} {avg:6.2f}")
    print(f"\nclass average: {class_avg:.2f}")
    print("by subject:   " + ", ".join(f"{k} {v:.1f}" for k, v in by_subject.items()))
    chart(per_student, class_avg, a.out)
    print(f"chart written to {a.out}")


if __name__ == "__main__":
    main()
