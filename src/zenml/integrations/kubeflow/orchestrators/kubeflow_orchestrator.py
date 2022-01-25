#  Copyright (c) ZenML GmbH 2021. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.

import os
import sys
from typing import TYPE_CHECKING, Any, Optional, Set

import kfp
import urllib3
from kubernetes import config

import zenml.io.utils
from zenml.enums import OrchestratorFlavor, StackComponentType
from zenml.exceptions import ProvisioningError
from zenml.integrations.kubeflow.orchestrators import local_deployment_utils
from zenml.integrations.kubeflow.orchestrators.kubeflow_dag_runner import (
    KubeflowDagRunner,
    KubeflowDagRunnerConfig,
)
from zenml.integrations.kubeflow.orchestrators.local_deployment_utils import (
    KFP_VERSION,
)
from zenml.io import fileio
from zenml.logger import get_logger
from zenml.orchestrators import BaseOrchestrator
from zenml.orchestrators.utils import create_tfx_pipeline
from zenml.repository import Repository
from zenml.stack import StackValidator
from zenml.stack.stack_component_class_registry import (
    register_stack_component_class,
)
from zenml.utils import networking_utils

if TYPE_CHECKING:
    from zenml.pipelines.base_pipeline import BasePipeline
    from zenml.runtime_configuration import RuntimeConfiguration
    from zenml.stack import Stack

logger = get_logger(__name__)

DEFAULT_KFP_UI_PORT = 8080


@register_stack_component_class(
    component_type=StackComponentType.ORCHESTRATOR,
    component_flavor=OrchestratorFlavor.KUBEFLOW,
)
class KubeflowOrchestrator(BaseOrchestrator):
    """Orchestrator responsible for running pipelines using Kubeflow."""

    custom_docker_base_image_name: Optional[str] = None
    kubeflow_pipelines_ui_port: int = DEFAULT_KFP_UI_PORT
    kubernetes_context: Optional[str] = None
    supports_local_execution = True
    supports_remote_execution = True

    @property
    def flavor(self) -> OrchestratorFlavor:
        """The orchestrator flavor."""
        return OrchestratorFlavor.KUBEFLOW

    @property
    def validator(self) -> Optional[StackValidator]:
        """Validates that the stack contains a container registry."""
        return StackValidator(
            required_components={StackComponentType.CONTAINER_REGISTRY}
        )

    def get_docker_image_name(self, pipeline_name: str) -> str:
        """Returns the full docker image name including registry and tag."""

        base_image_name = f"zenml-kubeflow:{pipeline_name}"
        container_registry = Repository().active_stack.container_registry

        if container_registry:
            registry_uri = container_registry.uri.rstrip("/")
            return f"{registry_uri}/{base_image_name}"
        else:
            return base_image_name

    @property
    def root_directory(self) -> str:
        """Returns path to the root directory for all files concerning
        this orchestrator."""
        return os.path.join(
            zenml.io.utils.get_global_config_directory(),
            "kubeflow",
            str(self.uuid),
        )

    @property
    def pipeline_directory(self) -> str:
        """Returns path to a directory in which the kubeflow pipeline files
        are stored."""
        return os.path.join(self.root_directory, "pipelines")

    def prepare_pipeline_deployment(
        self,
        pipeline: "BasePipeline",
        stack: "Stack",
        runtime_configuration: "RuntimeConfiguration",
    ) -> None:
        """Builds a docker image for the current environment and uploads it to
        a container registry if configured.
        """
        from zenml.integrations.kubeflow.docker_utils import (
            build_docker_image,
            push_docker_image,
        )

        image_name = self.get_docker_image_name(pipeline.name)

        requirements = {
            "kubernetes",
            *stack.requirements(
                exclude_components={StackComponentType.ORCHESTRATOR}
            ),
            *self._get_pipeline_requirements(pipeline),
        }

        logger.debug("Kubeflow docker container requirements: %s", requirements)

        build_docker_image(
            build_context_path=str(Repository().root),
            image_name=image_name,
            dockerignore_path=pipeline.dockerignore_file,
            requirements=requirements,
            base_image=self.custom_docker_base_image_name,
        )

        if stack.container_registry:
            push_docker_image(image_name)

    def run_pipeline(
        self, pipeline: "BasePipeline", stack: "Stack", run_name: str
    ) -> Any:
        """Runs a pipeline on Kubeflow Pipelines."""
        from zenml.integrations.kubeflow.docker_utils import get_image_digest

        image_name = self.get_docker_image_name(pipeline.name)
        image_name = get_image_digest(image_name) or image_name

        fileio.make_dirs(self.pipeline_directory)
        pipeline_file_path = os.path.join(
            self.pipeline_directory, f"{pipeline.name}.yaml"
        )
        runner_config = KubeflowDagRunnerConfig(image=image_name)
        runner = KubeflowDagRunner(
            config=runner_config, output_path=pipeline_file_path
        )
        tfx_pipeline = create_tfx_pipeline(pipeline, stack=stack)
        runner.run(tfx_pipeline)

        self._upload_and_run_pipeline(
            pipeline_file_path=pipeline_file_path,
            run_name=run_name,
            enable_cache=pipeline.enable_cache,
        )

    def _upload_and_run_pipeline(
        self, pipeline_file_path: str, run_name: str, enable_cache: bool
    ) -> None:
        """Tries to upload and run a KFP pipeline.

        Args:
            pipeline_file_path: Path to the pipeline definition file.
            run_name: A name for the pipeline run that will be started.
            enable_cache: Whether caching is enabled for this pipeline run.
        """
        try:
            if self.kubernetes_context:
                logger.info(
                    "Running in kubernetes context '%s'.",
                    self.kubernetes_context,
                )

            # load kubernetes config to authorize the KFP client
            config.load_kube_config(context=self.kubernetes_context)

            # upload the pipeline to Kubeflow and start it
            client = kfp.Client()
            result = client.create_run_from_pipeline_package(
                pipeline_file_path,
                arguments={},
                run_name=run_name,
                enable_caching=enable_cache,
            )
            logger.info("Started pipeline run with ID '%s'.", result.run_id)
        except urllib3.exceptions.HTTPError as error:
            logger.warning(
                "Failed to upload Kubeflow pipeline: %s. "
                "Please make sure your kube config is configured and the "
                "current context is set correctly.",
                error,
            )

    def _get_pipeline_requirements(self, pipeline: "BasePipeline") -> Set[str]:
        """Gets list of requirements for a pipeline."""
        if pipeline.requirements_file and fileio.file_exists(
            pipeline.requirements_file
        ):
            logger.debug(
                "Using requirements from file %s.", pipeline.requirements_file
            )
            with fileio.open(pipeline.requirements_file, "r") as f:
                return {
                    requirement.strip() for requirement in f.read().split("\n")
                }
        else:
            return set()

    @property
    def _pid_file_path(self) -> str:
        """Returns path to the daemon PID file."""
        return os.path.join(self.root_directory, "kubeflow_daemon.pid")

    @property
    def log_file(self) -> str:
        """Path of the daemon log file."""
        return os.path.join(self.root_directory, "kubeflow_daemon.log")

    @property
    def _k3d_cluster_name(self) -> str:
        """Returns the K3D cluster name."""
        # K3D only allows cluster names with up to 32 characters, use the
        # first 8 chars of the orchestrator UUID as identifier
        return f"zenml-kubeflow-{str(self.uuid)[:8]}"

    def _get_k3d_registry_name(self, port: int) -> str:
        """Returns the K3D registry name."""
        return f"k3d-zenml-kubeflow-registry.localhost:{port}"

    @property
    def _k3d_registry_config_path(self) -> str:
        """Returns the path to the K3D registry config yaml."""
        return os.path.join(self.root_directory, "k3d_registry.yaml")

    def list_manual_setup_steps(
        self, container_registry_name: str, container_registry_path: str
    ) -> None:
        """Logs manual steps needed to setup the Kubeflow local orchestrator."""
        global_config_dir_path = zenml.io.utils.get_global_config_directory()
        kubeflow_commands = [
            f"> k3d cluster create CLUSTER_NAME --registry-create {container_registry_name} --registry-config {container_registry_path} --volume {global_config_dir_path}:{global_config_dir_path}\n",
            f"> kubectl --context CLUSTER_NAME apply -k github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref={KFP_VERSION}&timeout=1m",
            "> kubectl --context CLUSTER_NAME wait --timeout=60s --for condition=established crd/applications.app.k8s.io",
            f"> kubectl --context CLUSTER_NAME apply -k github.com/kubeflow/pipelines/manifests/kustomize/env/platform-agnostic-pns?ref={KFP_VERSION}&timeout=1m",
            f"> kubectl --namespace kubeflow port-forward svc/ml-pipeline-ui {self.kubeflow_pipelines_ui_port}:80",
        ]

        logger.error("Unable to spin up local Kubeflow Pipelines deployment.")
        logger.info(
            "If you wish to spin up this Kubeflow local orchestrator manually, "
            "please enter the following commands (substituting where appropriate):\n"
        )
        logger.info("\n".join(kubeflow_commands))

    @property
    def is_provisioned(self) -> bool:
        """Returns if a local k3d cluster for this orchestrator exists."""
        if not local_deployment_utils.check_prerequisites():
            # if any prerequisites are missing there is certainly no
            # local deployment running
            return False

        return local_deployment_utils.k3d_cluster_exists(
            cluster_name=self._k3d_cluster_name
        )

    @property
    def is_running(self) -> bool:
        """Returns if the local k3d cluster for this orchestrator is running."""
        if not self.is_provisioned:
            return False

        return local_deployment_utils.k3d_cluster_running(
            cluster_name=self._k3d_cluster_name
        )

    def provision(self) -> None:
        """Provisions a local Kubeflow Pipelines deployment."""
        if self.is_running:
            logger.info(
                "Found already existing local Kubeflow Pipelines deployment. "
                "If there are any issues with the existing deployment, please "
                "run 'zenml orchestrator down' to delete it."
            )
            return

        if not local_deployment_utils.check_prerequisites():
            logger.error(
                "Unable to provision local Kubeflow Pipelines deployment: "
                "Please install 'k3d' and 'kubectl' and try again."
            )
            return

        container_registry = Repository().active_stack.container_registry
        if not container_registry:
            logger.error(
                "Unable to provision local Kubeflow Pipelines deployment: "
                "Missing container registry in current stack."
            )
            return

        logger.info("Provisioning local Kubeflow Pipelines deployment...")
        fileio.make_dirs(self.root_directory)
        container_registry_port = int(container_registry.uri.split(":")[-1])
        container_registry_name = self._get_k3d_registry_name(
            port=container_registry_port
        )
        local_deployment_utils.write_local_registry_yaml(
            yaml_path=self._k3d_registry_config_path,
            registry_name=container_registry_name,
            registry_uri=container_registry.uri,
        )

        try:
            local_deployment_utils.create_k3d_cluster(
                cluster_name=self._k3d_cluster_name,
                registry_name=container_registry_name,
                registry_config_path=self._k3d_registry_config_path,
            )
            kubernetes_context = f"k3d-{self._k3d_cluster_name}"
            local_deployment_utils.deploy_kubeflow_pipelines(
                kubernetes_context=kubernetes_context
            )

            port = self.kubeflow_pipelines_ui_port
            if (
                port == DEFAULT_KFP_UI_PORT
                and not networking_utils.port_available(port)
            ):
                # if the user didn't specify a specific port and the default
                # port is occupied, fallback to a random open port
                port = networking_utils.find_available_port()

            local_deployment_utils.start_kfp_ui_daemon(
                pid_file_path=self._pid_file_path,
                log_file_path=self.log_file,
                port=port,
            )
        except Exception as e:
            logger.error(e)
            self.list_manual_setup_steps(
                container_registry_name, self._k3d_registry_config_path
            )
            self.deprovision()

    def deprovision(self) -> None:
        """Deprovisions a local Kubeflow Pipelines deployment."""
        if self.is_running:
            local_deployment_utils.delete_k3d_cluster(
                cluster_name=self._k3d_cluster_name
            )

        if fileio.file_exists(self._pid_file_path):
            if sys.platform == "win32":
                # Daemon functionality is not supported on Windows, so the PID
                # file won't exist. This if clause exists just for mypy to not
                # complain about missing functions
                pass
            else:
                from zenml.utils import daemon

                daemon.stop_daemon(self._pid_file_path, kill_children=True)
                fileio.remove(self._pid_file_path)

        if fileio.file_exists(self.log_file):
            fileio.remove(self.log_file)

        logger.info("Local kubeflow pipelines deployment deprovisioned.")

    def resume(self) -> None:
        """Resumes the local k3d cluster."""
        if self.is_running:
            logger.info("Local kubeflow pipelines deployment already running.")
            return

        if not self.is_provisioned:
            raise ProvisioningError(
                "Unable to resume local kubeflow pipelines deployment: No "
                "resources provisioned for local deployment."
            )

        local_deployment_utils.start_k3d_cluster(
            cluster_name=self._k3d_cluster_name
        )

    def suspend(self) -> None:
        """Suspends the local k3d cluster."""
        if not self.is_running:
            logger.info("Local kubeflow pipelines deployment not running.")
            return

        local_deployment_utils.stop_k3d_cluster(
            cluster_name=self._k3d_cluster_name
        )