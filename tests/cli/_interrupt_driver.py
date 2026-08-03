import asyncio
from pathlib import Path
import sys

from scriptorium import cli


class BlockingService:
    def __init__(self, ready_path: Path) -> None:
        self.ready_path = ready_path

    async def start_run(self, revision, profile, budget_usd):
        self.ready_path.write_text("ready\n", encoding="utf-8")
        await asyncio.Event().wait()


def main() -> int:
    ready_path = Path(sys.argv[1])

    def find_repo(_path):
        return Path.cwd()

    def build_service(_repo):
        return BlockingService(ready_path)

    cli.find_repo = find_repo
    cli._build_service = build_service
    return cli.main(["--json", "run", "start", "--profile", "quick"])


if __name__ == "__main__":
    raise SystemExit(main())
