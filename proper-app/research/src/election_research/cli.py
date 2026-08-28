from __future__ import annotations

import argparse
import json
from pathlib import Path

from .protocol_cloud_v3 import Parameters, Point, analyze


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run the versioned physical-UIK protocol-cloud analysis"
    )
    result.add_argument("points", type=Path, help="JSON file containing an API points array")
    result.add_argument("--core-fraction", type=float, default=0.5)
    result.add_argument("--core-iterations", type=int, default=8)
    result.add_argument("--covariance-ridge", type=float, default=1e-4)
    result.add_argument("--fdr", type=float, default=0.05)
    result.add_argument("--review-threshold", type=float, default=0.999)
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
            tik_tvd=str(item["tikTvd"]),
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
            core_fraction=args.core_fraction,
            core_iterations=args.core_iterations,
            covariance_ridge=args.covariance_ridge,
            fdr_threshold=args.fdr,
            review_threshold=args.review_threshold,
        ),
    ).as_dict()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
