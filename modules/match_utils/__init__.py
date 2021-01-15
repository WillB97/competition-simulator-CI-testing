import os
import shutil
import argparse
import tempfile
import contextlib
import subprocess
from typing import List, Iterator, Optional
from pathlib import Path
from zipfile import ZipFile

import controller_utils  # isort:skip


class Match:
    def get_zone_path(self, zone_id: int) -> Path:
        robot_file: Path = controller_utils.get_zone_robot_file_path(zone_id)
        return robot_file.parent

    def construct_match_data(self, args: argparse.Namespace) -> controller_utils.MatchData:
        tlas = [
            tla if tla != '-' else None
            for tla in args.tla
        ]

        return controller_utils.MatchData(
            args.match_num,
            tlas,
            args.duration,
            recording_config=None,  # use the default
        )

    @contextlib.contextmanager
    def temporary_arena_root(self, suffix: str) -> Iterator[None]:
        original_arena_root = controller_utils.ARENA_ROOT
        with tempfile.TemporaryDirectory(suffix=suffix) as tmpdir_name:
            print(f"Using {tmpdir_name!r} as the arena")  # noqa:T001
            os.environ['ARENA_ROOT'] = tmpdir_name
            controller_utils.ARENA_ROOT = Path(tmpdir_name)
            try:
                yield
            finally:
                os.environ['ARENA_ROOT'] = str(original_arena_root)
                controller_utils.ARENA_ROOT = original_arena_root

    def prepare_match(
        self,
        archives_dir: Path,
        match_data: controller_utils.MatchData,
    ) -> None:
        controller_utils.get_mode_file().write_text('comp\n')
        controller_utils.record_match_data(match_data)

        for zone_id, tla in enumerate(match_data.teams):
            zone_path = self.get_zone_path(zone_id)

            if zone_path.exists():
                shutil.rmtree(zone_path)

            if tla is None:
                # no team in this zone
                continue

            zone_path.mkdir()
            with ZipFile(f'{archives_dir / tla}.zip') as zipfile:
                zipfile.extractall(zone_path)

    def print_error(self, message: str, *, strong: bool = False) -> None:
        BOLD = '\033[1m' if strong else ''
        FAIL = '\033[91m'
        ENDC = '\033[0m'
        return print(f"{BOLD}{FAIL}{message}{ENDC}")  # noqa:T001

    def print_fatal(self, message: str) -> None:
        return self.print_error(message, strong=True)

    def run_match(self) -> None:
        try:
            subprocess.check_call([
                'webots',
                '--batch',
                '--stdout',
                '--stderr',
                '--minimize',
                '--mode=realtime',
                str(controller_utils.REPO_ROOT / 'worlds' / 'Arena.wbt'),
            ])
        except FileNotFoundError:
            self.print_fatal(
                "Could not find webots. Check that you have it installed and on your PATH",
            )
            exit(1)
        except subprocess.CalledProcessError as e:
            try:
                log_text = controller_utils.get_competition_supervisor_log_filepath().read_text()  # noqa: E501
            except FileNotFoundError:
                self.print_fatal(
                    f"Simulation errored (exit code {e.returncode}). "
                    "No supervisor logs were found - Webots may have crashed.",
                )
            else:
                # There are potentially a large number of lines in the logs and any
                # errors are likely to be at the end of the logs. Additionally, the
                # user's focus is initially likely to be at the end of the ouptput.
                # To ensure that failures are clear we therefore put the message that
                # there was a failure last in the output (and in bold).
                self.print_error(log_text)
                self.print_fatal(
                    f"Simulation errored (exit code {e.returncode}). "
                    f"Competition supervisor logs are above.",
                )

            exit(1)

    def collate_logs(
        self,
        archives_dir: Path,
        match_data: controller_utils.MatchData,
    ) -> None:
        """
        Copy the teams' logs into directories next to their original code archives.
        """

        for zone_id, tla in enumerate(match_data.teams):
            if tla is None:
                # no team in this zone
                continue

            log_filname = controller_utils.get_robot_log_filename(zone_id)
            log_path = self.get_zone_path(zone_id) / log_filname

            team_dir = archives_dir / tla
            team_dir.mkdir(exist_ok=True)

            shutil.copy(log_path, team_dir)

    def archive_match_file(
        self,
        archives_dir: Path,
        match_data: controller_utils.MatchData,
    ) -> None:
        """
        Copy the match file (which contains the scoring data) to the archives directory.

        This also renames the file to the name which SRComp expects.
        """

        matches_dir = archives_dir / 'matches'
        matches_dir.mkdir(exist_ok=True)

        # The file contains JSON data, we're relying here on the fact that JSON is a
        # strict subset of YAML.
        completed_match_file = matches_dir / f'{match_data.match_number:0>3}.yaml'

        shutil.copy(controller_utils.get_match_file(), completed_match_file)

    def archive_match_recordings(self, archives_dir: Path) -> None:
        """
        Copy the recordings to the archives directory.
        """

        recordings_dir = archives_dir / 'recordings'
        recordings_dir.mkdir(exist_ok=True)

        recording_stem = controller_utils.get_recording_stem()

        # We don't want to rely on the recordings going into a directory which only
        # contains recording data, so instead we copy explicitly the things which we
        # know will have been output.

        for path in recording_stem.parent.glob(f'{recording_stem.name}.*'):
            shutil.copy(path, recordings_dir)

        try:
            shutil.copytree(recording_stem.parent / 'textures', recordings_dir / 'textures')
        except FileExistsError:
            # The textures are always the same, so we don't really care if they're
            # already there.
            pass

    def parse_args(self, args: Optional[List[str]] = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            'archives_dir',
            help=(
                "The directory containing the teams' robot code, as Zip archives "
                "named for the teams' TLAs. This directory will also be used as the "
                "root for storing the resulting logs and recordings."
            ),
            type=Path,
        )
        parser.add_argument(
            'match_num',
            type=int,
            help="The number of the match to run.",
        )
        parser.add_argument(
            'tla',
            nargs=controller_utils.NUM_ZONES,
            help=(
                "TLA of the team in each zone, in order from zone 0 to "
                f"{controller_utils.NUM_ZONES - 1}. Use dash (-) for an empty zone. "
                "Must specify all zones."
            ),
        )
        parser.add_argument(
            '--duration',
            help="The duration of the match (in seconds).",
            type=int,
            default=controller_utils.GAME_DURATION_SECONDS,
        )
        # setting args to None uses the command line arguments
        return parser.parse_args(args)

    def main(self, args: argparse.Namespace) -> None:
        match_data = self.construct_match_data(args)

        with self.temporary_arena_root(f'match-{match_data.match_number}'):
            self.prepare_match(args.archives_dir, match_data)

            self.run_match()

            self.collate_logs(args.archives_dir, match_data)
            self.archive_match_file(args.archives_dir, match_data)
            self.archive_match_recordings(args.archives_dir)
