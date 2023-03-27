"""
Manage the configuration and dynamic build state for the Census build.

"""
import functools
import io
import os
import pathlib
from datetime import datetime
from typing import Any, Iterator, Mapping, Union

import attrs
import psutil
import yaml
from typing_extensions import Self

"""
Defaults for Census configuration.
"""

CENSUS_BUILD_CONFIG = "config.yaml"
CENSUS_BUILD_STATE = "state.yaml"
CENSUS_CONFIG_DEFAULTS = {
    # General config
    "verbose": 1,
    "log_dir": "logs",
    "log_file": "build.log",
    "consolidate": True,
    "disable_dirty_git_check": True,
    #
    # Paths and census version name determined by spec.
    "cell_census_S3_path": "s3://cellxgene-data-public/cell-census",
    "build_tag": datetime.now().astimezone().date().isoformat(),
    #
    # Default multi-process. Memory scaling based on empirical tests.
    "multi_process": True,
    "max_workers": 2 + int(psutil.virtual_memory().total / (96 * 1024**3)),
    #
    # Host minimum resource validation
    "host_validation_disable": False,  # if True, host validation checks will be skipped
    "host_validation_min_physical_memory": 512 * 1024**3,  # 512GiB
    "host_validation_min_swap_memory": 2 * 1024**4,  # 2TiB
    "host_validation_min_free_disk_space": 1 * 1024**4,  # 1 TiB
    #
    # For testing convenience only
    "manifest": None,
    "test_first_n": None,
}


class Namespace(Mapping[str, Any]):
    """Readonly namespace"""

    def __init__(self, **kwargs: Any):
        self._state = dict(kwargs)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Namespace):
            return self._state == other._state
        return NotImplemented

    def __contains__(self, key: Any) -> bool:
        return key in self._state

    def __repr__(self) -> str:
        items = (f"{k}={v!r}" for k, v in self.items())
        return "{}({})".format(type(self).__name__, ", ".join(items))

    def __getitem__(self, key: str) -> Any:
        return self._state[key]

    def __getattr__(self, key: str) -> Any:
        return self._state[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._state)

    def __len__(self) -> int:
        return len(self._state)

    def __getstate__(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)


class MutableNamespace(Namespace):
    """Mutable namespace"""

    def __setitem__(self, key: str, value: Any) -> None:
        if not isinstance(key, str):
            raise TypeError
        self._state[key] = value

    # Do not implement __delitem__. Log format has no deletion marker, so delete
    # semantics can't be supported until that is implemented.


class CensusBuildConfig(Namespace):
    defaults = CENSUS_CONFIG_DEFAULTS

    def __init__(self, **kwargs: Any):
        config = self.defaults.copy()
        config.update(kwargs)
        super().__init__(**config)

    @classmethod
    def load(cls, file: Union[str, os.PathLike[str], io.TextIOBase]) -> Self:
        if isinstance(file, (str, os.PathLike)):
            with open(file) as f:
                user_config = yaml.safe_load(f)
        else:
            user_config = yaml.safe_load(file)

        # Empty YAML config file is legal
        if user_config is None:
            user_config = {}

        # But we only understand a top-level dictionary (e.g., no lists, etc.)
        if not isinstance(user_config, dict):
            raise TypeError("YAML config file malformed - expected top-level dictionary")

        return cls(**user_config)


class CensusBuildState(MutableNamespace):
    def __init__(self, **kwargs: Any):
        self.__dirty_keys = set(kwargs)
        super().__init__(**kwargs)

    def __setitem__(self, key: str, value: Any) -> None:
        if self._state.get(key) == value:
            return
        super().__setitem__(key, value)
        self.__dirty_keys.add(key)

    @classmethod
    def load(cls, file: Union[str, os.PathLike[str], io.TextIOBase]) -> Self:
        if isinstance(file, (str, os.PathLike)):
            with open(file) as state_log:
                documents = list(yaml.safe_load_all(state_log))
        else:
            documents = list(yaml.safe_load_all(file))

        state = cls(**functools.reduce(lambda acc, r: acc.update(r) or acc, documents, {}))
        state.__dirty_keys.clear()
        return state

    def commit(self, file: Union[str, os.PathLike[str]]) -> None:
        # append dirty elements (atomic on Posix)
        if self.__dirty_keys:
            dirty = {k: self[k] for k in self.__dirty_keys}
            self.__dirty_keys.clear()
            with open(file, mode="a") as state_log:
                record = f"--- # {datetime.now().isoformat()}\n" + yaml.dump(dirty)
                state_log.write(record)


@attrs.define(frozen=True)
class CensusBuildArgs:
    working_dir: pathlib.PosixPath = attrs.field(validator=attrs.validators.instance_of(pathlib.PosixPath))
    config: CensusBuildConfig = attrs.field(validator=attrs.validators.instance_of(CensusBuildConfig))
    state: CensusBuildState = attrs.field(
        factory=CensusBuildState, validator=attrs.validators.instance_of(CensusBuildState)  # default: empty state
    )

    @property
    def soma_path(self) -> pathlib.PosixPath:
        return self.working_dir / self.build_tag / "soma"

    @property
    def h5ads_path(self) -> pathlib.PosixPath:
        return self.working_dir / self.build_tag / "h5ads"

    @property
    def build_tag(self) -> str:
        build_tag = self.config.build_tag
        if not isinstance(build_tag, str):
            raise TypeError("Configuration contains non-string build_tag.")
        return build_tag