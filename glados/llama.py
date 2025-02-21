import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self, Sequence

import requests
import yaml

log = logging.getLogger(__name__)


class ServerStartupError(RuntimeError):
    pass


@dataclass
class LlamaServerConfig:
    llama_cpp_repo_path: str
    model_path: str
    port: int = 8080
    use_gpu: bool = True

    @classmethod
    def from_yaml(
        cls, path: str, key_to_config: Sequence[str] | None = ("LlamaServer",)
    ) -> Self | None:
        key_to_config = key_to_config or []

        with open(path, "r") as file:
            data = yaml.safe_load(file)

        config = data
        for nested_key in key_to_config:
            config = config.get(nested_key, {})
        if not config:
            return None
        return cls(**config)


# TODO: extract abstract LLMServer class
class LlamaServer:
    def __init__(
        self,
        llama_cpp_repo_path: Path,
        model_path: Path,
        port=8080,
        use_gpu: bool = True,
    ):
        self.llama_cpp_repo_path = llama_cpp_repo_path
        self.model_path = model_path

        self.port = port
        self.process: subprocess.Popen | None = None
        self.use_gpu = use_gpu

        self.command = [self.llama_cpp_repo_path, "-m"] + [self.model_path]
        if self.use_gpu:
            self.command += ["-ngl", "1000"]

    @classmethod
    def from_config(cls, config: LlamaServerConfig):
        llama_cpp_repo_path = Path(config.llama_cpp_repo_path) / "server"
        llama_cpp_repo_path = llama_cpp_repo_path.resolve()
        model_path = Path(config.model_path).resolve()

        return cls(
            llama_cpp_repo_path=llama_cpp_repo_path,
            model_path=model_path,
            port=config.port,
            use_gpu=config.use_gpu,
        )

    @property
    def base_url(self):
        return f"http://localhost:{self.port}"

    @property
    def completion_url(self):
        return f"{self.base_url}/completion"

    @property
    def health_check_url(self):
        return f"{self.base_url}/health"

    def start(self):
        log.info(f"Starting the server by executing command {self.command=}")
        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if not self.is_running():
            self.stop()
            raise ServerStartupError("Failed to startup! Check the error log messages")

    def is_running(
        self,
        max_connection_attempts: int = 10,
        sleep_time_between_attempts: float = 0.01,
        max_wait_time_for_model_loading: float = 60.0,
    ) -> bool:
        if self.process is None:
            return False

        cur_attempt = 0
        model_loading_time = 0
        model_loading_log_time = 1
        while True:
            try:
                response = requests.get(self.health_check_url)

                if response.status_code == 503:
                    if model_loading_time > max_wait_time_for_model_loading:
                        log.error(
                            f"Model failed to load in {max_wait_time_for_model_loading}. "
                            f"Consider increasing the waiting time for model loading."
                        )
                        return False

                    log.info(
                        f"model is still being loaded, or at full capacity. "
                        f"Will wait for {max_wait_time_for_model_loading - model_loading_time} "
                        f"more seconds: {response=}"
                    )
                    time.sleep(model_loading_log_time)
                    model_loading_time += model_loading_log_time
                    continue
                if response.status_code == 200:
                    log.debug(f"Server started successfully, {response=}")
                    return True
                log.error(
                    f"Server is not responding properly, maybe model failed to load: {response=}"
                )
                return False

            except requests.exceptions.ConnectionError:
                log.debug(
                    f"Couldn't establish connection, retrying with attempt: {cur_attempt}/{max_connection_attempts}"
                )
                cur_attempt += 1
                if cur_attempt > max_connection_attempts:
                    log.error(
                        f"Couldn't establish connection after {max_connection_attempts=}"
                    )
                    return False
            time.sleep(sleep_time_between_attempts)

    def stop(self):
        self.process.terminate()
        self.process.wait()
        self.process = None

    def __del__(self):
        self.stop()
        del self
