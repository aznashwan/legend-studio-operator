#!/usr/bin/env python3
# Copyright 2021 Canonical
# See LICENSE file for licensing details.

""" Module defining the Charmed operator for the FINOS Legend Studio. """

import json
import logging
import subprocess

from ops import charm
from ops import framework
from ops import main
from ops import model

from charms.finos_legend_db_k8s.v0 import legend_database
from charms.finos_legend_gitlab_integrator_k8s.v0 import legend_gitlab
from charms.nginx_ingress_integrator.v0 import ingress


logger = logging.getLogger(__name__)

STUDIO_UI_CONFIG_FILE_CONTAINER_LOCAL_PATH = "/ui-config.json"
STUDIO_HTTP_CONFIG_FILE_CONTAINER_LOCAL_PATH = "/http-config.json"

APPLICATION_SERVER_UI_PATH = "/studio"
STUDIO_SERVICE_URL_FORMAT = "%(schema)s://%(host)s:%(port)s%(path)s"
STUDIO_GITLAB_REDIRECT_URI_FORMAT = "%(base_url)s/log.in/callback"

APPLICATION_CONNECTOR_TYPE_HTTP = "http"
APPLICATION_CONNECTOR_PORT_HTTP = 8080
APPLICATION_CONNECTOR_TYPE_HTTPS = "https"
APPLICATION_CONNECTOR_PORT_HTTPS = 8081

VALID_APPLICATION_LOG_LEVEL_SETTINGS = [
    "INFO", "WARN", "DEBUG", "TRACE", "OFF"]

GITLAB_REQUIRED_SCOPES = ["openid", "profile", "api"]


class LegendStudioServerOperatorCharm(charm.CharmBase):
    """ Charmed operator for the FINOS Legend Studio. """

    _stored = framework.StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._set_stored_defaults()

        self._legend_db_consumer = legend_database.LegendDatabaseConsumer(
            self)
        self._legend_gitlab_consumer = legend_gitlab.LegendGitlabConsumer(
            self, relation_name="legend-studio-gitlab")
        self.ingress = ingress.IngressRequires(
            self, {
                "service-hostname": self.app.name,
                "service-name": self.app.name,
                "service-port": APPLICATION_CONNECTOR_PORT_HTTP})

        # Standard charm lifecycle events:
        self.framework.observe(
            self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.on.studio_pebble_ready, self._on_studio_pebble_ready)

        # DB relation lifecycle events:
        self.framework.observe(
            self.on["legend-db"].relation_joined,
            self._on_db_relation_joined)
        self.framework.observe(
            self.on["legend-db"].relation_changed,
            self._on_db_relation_changed)

        # GitLab integrator lifecycle:
        self.framework.observe(
            self.on["legend-studio-gitlab"].relation_joined,
            self._on_legend_gitlab_relation_joined)
        self.framework.observe(
            self.on["legend-studio-gitlab"].relation_changed,
            self._on_legend_gitlab_relation_changed)

        # SDLC relation events:
        self.framework.observe(
            self.on["legend-sdlc"].relation_joined,
            self._on_sdlc_relation_joined)
        self.framework.observe(
            self.on["legend-sdlc"].relation_changed,
            self._on_sdlc_relation_changed)

        # Engine relation events:
        self.framework.observe(
            self.on["legend-engine"].relation_joined,
            self._on_engine_relation_joined)
        self.framework.observe(
            self.on["legend-engine"].relation_changed,
            self._on_engine_relation_changed)

    def _set_stored_defaults(self) -> None:
        self._stored.set_default(log_level="DEBUG")
        self._stored.set_default(legend_db_credentials={})
        self._stored.set_default(legend_gitlab_credentials={})
        self._stored.set_default(sdlc_service_url="")
        self._stored.set_default(engine_service_url="")

    def _on_studio_pebble_ready(self, event: framework.EventBase) -> None:
        """Define the Studio workload using the Pebble API.
        Note that this will *not* start the service, but instead leave it in a
        blocked state until the relevant relations required for it are added.
        """
        # Get a reference the container attribute on the PebbleReadyEvent
        container = event.workload

        # Define an initial Pebble layer configuration
        pebble_layer = {
            "summary": "Studio layer.",
            "description": "Pebble config layer for FINOS Legend Studio.",
            "services": {
                "studio": {
                    "override": "replace",
                    "summary": "studio",
                    "command": (
                        # NOTE(aznashwan): starting through bash is required
                        # for the classpath glob (-cp ...) to be expanded:
                        "/bin/sh -c 'java -XX:+ExitOnOutOfMemoryError -Xss4M "
                        "-XX:MaxRAMPercentage=60 -Dfile.encoding=UTF8 -cp "
                        "/app/bin/webapp-content:/app/bin/* "
                        "org.finos.legend.server.shared.staticserver.Server "
                        "server %s'" % (
                            STUDIO_HTTP_CONFIG_FILE_CONTAINER_LOCAL_PATH)
                    ),
                    # NOTE(aznashwan): considering the Studio service expects
                    # a singular config file which already contains all
                    # relevant options in it (some of which will require the
                    # relation with DB/GitLab to have already been
                    # established), we do not auto-start:
                    "startup": "disabled",
                    # TODO(aznashwan): determine any env vars we could pass
                    # (most notably, things like the RAM percentage etc...)
                    "environment": {},
                }
            },
        }

        # Add intial Pebble config layer using the Pebble API
        container.add_layer("studio", pebble_layer, combine=True)

        # NOTE(aznashwan): as mentioned above, we will *not* be auto-starting
        # the service until the relations with DBMan and GitLab are added:
        # container.autostart()

        self.unit.status = model.BlockedStatus(
            "requires relating to: finos-legend-db-k8s, "
            "finos-legend-gitlab-integrator-k8s")

    def _get_logging_level_from_config(self, option_name):
        """Fetches the config option with the given name and checks to
        ensure that it is a valid `java.utils.logging` log level.

        Returns None if an option is invalid.
        """
        value = self.model.config[option_name]
        if value not in VALID_APPLICATION_LOG_LEVEL_SETTINGS:
            logger.warning(
                "Invalid Java logging level value provided for option "
                "'%s': '%s'. Valid Java logging levels are: %s. The charm "
                "shall block until a proper value is set.",
                option_name, value, VALID_APPLICATION_LOG_LEVEL_SETTINGS)
            return None
        return value

    def _add_ui_config_from_relation_data(self, ui_config):
        """This method adds all relevant Studio UI config options into the
        provided dict to be directly rendered to JSON and passed to the Studio.

        Returns:
            None if all of the config options derived from the config/relations
            are present and have passed Charm-side valiation steps.
            A `model.BlockedStatus` instance with a relevant message otherwise.
        """
        sdlc_url = self._stored.sdlc_service_url
        if not sdlc_url:
            return model.BlockedStatus(
                "requires relating to: finos-legend-sdlc-k8s, "
                "finos-legend-engine-k8s")

        engine_url = self._stored.engine_service_url
        if not engine_url:
            return model.BlockedStatus(
                "requires relating to: finos-legend-engine-k8s")

        # TODO(aznashwan): fill in the URLs from relation data:
        ui_config.update({
            "appName": "studio",
            "env": "test",
            "sdlc": {
                "url": sdlc_url
            },
            "metadata": {
                "url": "__LEGEND_DEPOT_URL__/api"
            },
            "engine": {
                "url": engine_url
            },
            "documentation": {
                "url": "https://legend.finos.org"
            },
            "options": {
                "core": {
                    # TODO(aznashwan): could this error in the future?
                    "TEMPORARY__disableServiceRegistration": True
                }
            }
        })

    def _add_base_service_config_from_charm_config(
            self, studio_http_config: dict = {}) -> model.BlockedStatus:
        """This method adds all relevant Studio config options into the
        provided dict to be directly rendered to JSON and passed to the Studio.

        Returns:
            None if all of the config options derived from the config/relations
            are present and have passed Charm-side valiation steps.
            A `model.BlockedStatus` instance with a relevant message otherwise.
        """
        # Check Mongo-related options:
        mongo_creds = self._stored.legend_db_credentials
        if not mongo_creds:
            return model.BlockedStatus(
                "requires relating to: finos-legend-db-k8s")

        # Check GitLab-related options:
        legend_gitlab_creds = self._stored.legend_gitlab_credentials
        if not legend_gitlab_creds:
            return model.BlockedStatus(
                "requires relating to: finos-legend-gitlab-integrator-k8s")
        gitlab_client_id = legend_gitlab_creds['client_id']
        gitlab_client_secret = legend_gitlab_creds[
            'client_secret']
        gitlab_openid_discovery_url = legend_gitlab_creds[
            'openid_discovery_url']

        # Check Java logging options:
        pac4j_logging_level = self._get_logging_level_from_config(
            "server-pac4j-logging-level")
        server_logging_level = self._get_logging_level_from_config(
            "server-logging-level")
        if not all([
                server_logging_level, pac4j_logging_level]):
            return model.BlockedStatus(
                "one or more logging config options are improperly formatted "
                "or missing, please review the debug-log for more details")

        # Compile base config:
        studio_http_config.update({
            "uiPath": APPLICATION_SERVER_UI_PATH,
            "html5Router": True,
            "server": {
                "type": "simple",
                "applicationContextPath": "/",
                "adminContextPath": "%s/admin" % APPLICATION_SERVER_UI_PATH,
                "connector": {
                    "type": APPLICATION_CONNECTOR_TYPE_HTTP,
                    "port": APPLICATION_CONNECTOR_PORT_HTTP
                }
            },
            "logging": {
                "level": server_logging_level,
                "loggers": {
                    "root": {"level": server_logging_level},
                    "org.pac4j": {"level": pac4j_logging_level}
                },
                "appenders": [{"type": "console"}]
            },
            "pac4j": {
                "callbackPrefix": "/studio/log.in",
                "bypassPaths": ["/studio/admin/healthcheck"],
                "mongoUri": mongo_creds['uri'],
                "mongoDb": mongo_creds['database'],
                "clients": [{
                    "org.finos.legend.server.pac4j.gitlab.GitlabClient": {
                        "name": "gitlab",
                        "clientId": gitlab_client_id,
                        "secret": gitlab_client_secret,
                        "discoveryUri": gitlab_openid_discovery_url,
                        # NOTE(aznashwan): needs to be a space-separated str:
                        "scope": " ".join(GITLAB_REQUIRED_SCOPES)
                    }
                }],
                "mongoSession": {
                    "enabled": True,
                    "collection": "userSessions"
                }
            },
            # TODO(aznashwan): check if these are necessary:
            "routerExemptPaths": [
                "/editor.worker.js",
                "/json.worker.js",
                "/editor.worker.js.map",
                "/json.worker.js.map",
                "/version.json",
                "/config.json",
                "/favicon.ico",
                "/static"
            ],
            "localAssetPaths": {
                "/studio/config.json": (
                    STUDIO_UI_CONFIG_FILE_CONTAINER_LOCAL_PATH)
            },
        })

        return None

    def _add_config_file_to_container(
            self, container: model.Container, container_path: str,
            config: dict) -> None:
        """Serializes the provided config dict to JSON and adds it in the
        Studio service container under the provided path via Pebble API.
        """
        logger.debug(
            "Adding following config under '%s' in container: %s",
            container_path, config)
        container.push(
            container_path,
            json.dumps(config),
            make_dirs=True)
        logger.info(
            "Successfully wrote config file in container under '%s'",
            container_path)

    def _restart_studio_service(self, container: model.Container) -> None:
        """Restarts the Studio service using the Pebble container API.
        """
        logger.debug("Restarting Studio service")
        container.restart("studio")
        logger.debug("Successfully issues Studio service restart")

    def _reconfigure_studio_service(self) -> None:
        """Generates the JSON config for the Studio server and adds it
        into the container via Pebble files API.
        - regenerating the JSON config for the Studio server
        - regenerating the JSON config containing the Engine/SDLC URLs
        - adding said configs via Pebble
        - instructing Pebble to restart the Studio server
        The Studio is power-cycled for the new configuration to take effect.
        """
        config = {}
        possible_blocked_status = (
            self._add_base_service_config_from_charm_config(config))
        if possible_blocked_status:
            self.unit.status = possible_blocked_status
            return

        ui_config = {}
        possible_blocked_status = self._add_ui_config_from_relation_data(
            ui_config)
        if possible_blocked_status:
            self.unit.status = possible_blocked_status
            return

        container = self.unit.get_container("studio")
        if container.can_connect():
            logger.debug("Updating Studio service configuration")
            self._add_config_file_to_container(
                container,
                STUDIO_HTTP_CONFIG_FILE_CONTAINER_LOCAL_PATH,
                config)
            self._add_config_file_to_container(
                container,
                STUDIO_UI_CONFIG_FILE_CONTAINER_LOCAL_PATH,
                ui_config)
            self._restart_studio_service(container)
            self.unit.status = model.ActiveStatus()
            return

        logger.info("Studio container is not active yet. No config to update.")
        self.unit.status = model.BlockedStatus(
            "requires relating to: finos-legend-db-k8s, "
            "finos-legend-gitlab-integrator-k8s")

    def _get_studio_service_url(self):
        ip_address = subprocess.check_output(
            ["unit-get", "private-address"]).decode().strip()
        return STUDIO_SERVICE_URL_FORMAT % ({
            # NOTE(aznashwan): we always return the plain HTTP endpoint:
            "schema": "http",
            "host": ip_address,
            "port": APPLICATION_CONNECTOR_PORT_HTTP,
            "path": APPLICATION_SERVER_UI_PATH})

    def _on_config_changed(self, _) -> None:
        """Reacts to configuration changes to the service by:
        - regenerating the YAML config for the Studio server
        - adding it via Pebble
        - instructing Pebble to restart the Studio server
        """
        self._reconfigure_studio_service()

    def _on_db_relation_joined(self, event: charm.RelationJoinedEvent):
        logger.debug("No actions are to be performed during DB relation join")

    def _on_db_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        mongo_creds = self._legend_db_consumer.get_legend_database_creds(
            event.relation.id)
        if not mongo_creds:
            self.unit.status = model.WaitingStatus(
                "awaiting legend db relation data")
            event.defer()
            return
        logger.debug(
            "Mongo credentials returned by DB relation: %s",
            mongo_creds)
        self._stored.legend_db_credentials = mongo_creds

        # Attempt to reconfigure and restart the service with the new data:
        self._reconfigure_studio_service()

    def _on_legend_gitlab_relation_joined(
            self, event: charm.RelationJoinedEvent) -> None:
        base_url = self._get_studio_service_url()
        redirect_uris = [
            STUDIO_GITLAB_REDIRECT_URI_FORMAT % {"base_url": base_url}]

        legend_gitlab.set_legend_gitlab_redirect_uris_in_relation_data(
            event.relation.data[self.app], redirect_uris)

    def _on_legend_gitlab_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        gitlab_creds = None
        try:
            gitlab_creds = (
                self._legend_gitlab_consumer.get_legend_gitlab_creds(
                    event.relation.id))
        except Exception as ex:
            logger.exception(ex)
            self.unit.status = model.BlockedStatus(
                "failed to retrieve GitLab creds from relation data, "
                "ensure finos-legend-gitlab-integrator-k8s is compatible")
            return

        if not gitlab_creds:
            self.unit.status = model.WaitingStatus(
                "awaiting legend gitlab credentials from integrator")
            event.defer()
            return

        self._stored.legend_gitlab_credentials = gitlab_creds
        self._reconfigure_studio_service()

    def _on_sdlc_relation_joined(self, event: charm.RelationJoinedEvent):
        logger.debug("No actions are to be performed after SDLC relation join")

    def _on_sdlc_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        rel_id = event.relation.id
        rel = self.framework.model.get_relation("legend-sdlc", rel_id)
        sdlc_url = rel.data[event.app].get("legend-sdlc-url")
        if not sdlc_url:
            self.unit.status = model.WaitingStatus(
                "waiting for legend sdlc to report service URL.")
            return

        logger.info("### SDLC URL received from relation: %s", sdlc_url)
        self._stored.sdlc_service_url = sdlc_url

        # Attempt to reconfigure and restart the service with the new data:
        self._reconfigure_studio_service()

    def _on_engine_relation_joined(self, event: charm.RelationJoinedEvent):
        logger.debug(
            "No actions are to be performed after engine relation join")

    def _on_engine_relation_changed(
            self, event: charm.RelationChangedEvent) -> None:
        rel_id = event.relation.id
        rel = self.framework.model.get_relation("legend-engine", rel_id)
        engine_url = rel.data[event.app].get("legend-engine-url")
        if not engine_url:
            self.unit.status = model.WaitingStatus(
                "waiting for legend engine to report service url")
            return

        logger.info("### Engine URL received from relation: %s", engine_url)
        self._stored.engine_service_url = engine_url

        # Attempt to reconfigure and restart the service with the new data:
        self._reconfigure_studio_service()


if __name__ == "__main__":
    main.main(LegendStudioServerOperatorCharm)
