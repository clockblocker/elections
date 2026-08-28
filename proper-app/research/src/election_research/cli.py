from __future__ import annotations

import argparse
import json
from pathlib import Path

from .shpilkin_v1 import Parameters, Point, analyze


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the versioned physical-UIK analysis")
    result.add_argument("points", type=Path, help="JSON file containing an API points array")
    result.add_argument("--reference-min", type=float, default=20)
    result.add_argument("--reference-max", type=float, default=50)
    result.add_argument("--analysis-min", type=float, default=50)
    result.add_argument("--include-negative", action="store_true")
    result.add_argument("--output", type=Path)
    return result


def main() -> None:
    args = parser().parse_args()
    document = json.loads(args.points.read_text(encoding="utf-8"))
    raw_points = document["points"] if isinstance(document, dict) else document
    points = [
        Point(
            id=str(item["id"]),
            region_code=str(item["regionCode"]),
            registered_voters=int(item["registeredVoters"]),
            valid_ballots=int(item["validBallots"]),
            ballots_counted=int(item["ballotsCounted"]),
            option_votes=int(item["optionVotes"]),
        )
        for item in raw_points
    ]
    result = analyze(
        points,
        Parameters(
            reference_turnout_min=args.reference_min,
            reference_turnout_max=args.reference_max,
            analysis_turnout_min=args.analysis_min,
            positive_excess_only=not args.include_negative,
        ),
    ).as_dict()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
