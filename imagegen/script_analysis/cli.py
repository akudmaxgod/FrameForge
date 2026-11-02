"""CLI. Results go to a file; diagnostics and safe errors go to stderr."""
import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
from dotenv import load_dotenv
from .analyzer import Analyzer
from .balance import guard_balance
from .cache import DiskCache, atomic_json
from .errors import AnalysisError


def main() -> int:
    parser = argparse.ArgumentParser(description="Два прохода анализа сценария через DeepSeek Flash")
    parser.add_argument("script", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".script-analysis/cache"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--check-balance", action="store_true", help="Проверить минимум $0.50 перед обработкой")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Do not emit SDK HTTP diagnostics, even when the application logs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    load_dotenv(args.env_file, override=False, encoding="utf-8-sig")
    try:
        if args.output and (args.output.resolve() == args.script.resolve() or args.output.resolve() == args.env_file.resolve()):
            raise ValueError("Выходной файл не должен заменять сценарий или .env.")
        with args.script.open(encoding="utf-8-sig", newline="") as handle:
            script = handle.read()
        if args.check_balance:
            guard_balance()
        result = Analyzer(cache=DiskCache(args.cache_dir)).analyze(script, use_cache=not args.no_cache)
        if args.output:
            atomic_json(args.output, result.to_dict())
            print(json.dumps(asdict(result.stats), ensure_ascii=False))
        else:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except (AnalysisError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        if isinstance(exc, AnalysisError) and exc.stats is not None:
            print(json.dumps(asdict(exc.stats)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
