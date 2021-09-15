#!/usr/bin/env python3
# Copyright 2021 Canonical
# See LICENSE file for licensing details.

""" Module defining the Charmed operator for the FINOS Legend Studio. """

import json
import logging

from ops import charm
from ops import framework
from ops import main
from ops import model

logger = logging.getLogger(__name__)


STUDIO_UI_CONFIG_FILE_CONTAINER_LOCAL_PATH = "/ui-config.json"
STUDIO_HTTP_CONFIG_FILE_CONTAINER_LOCAL_PATH = "/http-config.json"

APPLICATION_CONNECTOR_TYPE_HTTP = "http"
APPLICATION_CONNECTOR_TYPE_HTTPS = "https"

VALID_APPLICATION_LOG_LEVEL_SETTINGS = [
    "INFO", "WARN", "DEBUG", "TRACE", "OFF"]

GITLAB_PROJECT_VISIBILITY_PUBLIC = "public"
GITLAB_PROJECT_VISIBILITY_PRIVATE = "private"
GITLAB_REQUIRED_SCOPES = ["openid", "profile", "api"]
GITLAB_OPENID_DISCOVERY_URL = (
    "https://gitlab.com/.well-known/openid-configuration")


class LegendStudioServerOperatorCharm(charm.CharmBase):
    """ Charmed operator for the FINOS Legend Studio. """

    _stored = framework.StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._set_stored_defaults()

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
        self._stored.set_default(mongodb_credentials={})
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
                    # relation with DB/Gitlab to have already been
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
        # the service until the relations with DBMan and Gitlab are added:
        # container.autostart()

        self.unit.status = model.BlockedStatus(
            "Awaiting Legend Database and Gitlab relations.")

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
                "Need Legend SDLC relation to configure the Studio service.")

        engine_url = self._stored.engine_service_url
        if not engine_url:
            return model.BlockedStatus(
                "Need Legend Engine relation to configure Studio service.")

        # TODO(aznashwan): fill in the URLs from relation data:
        ui_config.update({
            "appName": "studio",
            "env": "test",
            "sdlc": {
                "url": "http://10.107.9.20:7070/api"
            },
            "metadata": {
                "url": "__LEGEND_DEPOT_URL__/api"
            },
            "engine": {
                "url": "http://10.107.9.20:6060/api"
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
        # Check gitlab-related options:
        # TODO(aznashwan): remove this check on eventual Gitlab relation:
        gitlab_client_id = self.model.config.get('gitlab-client-id')
        gitlab_client_secret = self.model.config.get('gitlab-client-secret')
        if not all([
                gitlab_client_id, gitlab_client_secret]):
            return model.BlockedStatus(
                "One or more Gitlab-related charm configuration options "
                "are missing.")

        # Check Java logging options:
        pac4j_logging_level = self._get_logging_level_from_config(
            "server-pac4j-logging-level")
        server_logging_level = self._get_logging_level_from_config(
            "server-logging-level")
        if not all([
                server_logging_level, pac4j_logging_level]):
            return model.BlockedStatus(
                "One or more logging config options are improperly formatted "
                "or missing. Please review the debug-log for more details.")

        # Check Mongo-related options:
        mongo_creds = self._stored.mongodb_credentials
        if not mongo_creds or 'replica_set_uri' not in mongo_creds:
            return model.BlockedStatus(
                "No stored MongoDB credentials were found yet. Please "
                "ensure the Charm is properly related to MongoDB.")
        mongo_replica_set_uri = self._stored.mongodb_credentials[
            'replica_set_uri']
        databases = mongo_creds.get('databases')
        database_name = None
        if databases:
            database_name = databases[0]
            # NOTE(aznashwan): the Java MongoDB can't handle DB names in the
            # URL, so we need to trim that part and pass the database name
            # as a separate parameter within the config as the
            # studio_config['pac4j']['mongoDb'] option below.
            split_uri = [
                elem
                for elem in mongo_replica_set_uri.split('/')[:-1]
                # NOTE: filter any empty strings resulting from double-slashes:
                if elem]
            # NOTE: schema prefix needs two slashes added back:
            mongo_replica_set_uri = "%s//%s" % (
                split_uri[0], "/".join(split_uri[1:]))
        studio_ui_path = self.model.config["server-ui-path"]

        # Compile base config:
        studio_http_config.update({
            "uiPath": studio_ui_path,
            "html5Router": True,
            "server": {
              "type": "simple",
              "applicationContextPath": "/",
              "adminContextPath": "%s/admin" % studio_ui_path,
              "connector": {
                "type": APPLICATION_CONNECTOR_TYPE_HTTP,
                "port": self.model.config[
                    'server-application-connector-port-http']
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
                "mongoUri": mongo_replica_set_uri,
                "mongoDb": database_name,
                "clients": [{
                    "org.finos.legend.server.pac4j.gitlab.GitlabClient": {
                        "name": "gitlab",
                        "clientId": gitlab_client_id,
                        "secret": gitlab_client_secret,
                        "discoveryUri": GITLAB_OPENID_DISCOVERY_URL,
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
            self.unit.status = model.ActiveStatus(
                "Studio service has been started.")
            return

        logger.info("Studio container is not active yet. No config to update.")
        self.unit.status = model.BlockedStatus(
            "Awaiting Legend DB and Gitlab relations.")

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
        rel_id = event.relation.id
        rel = self.framework.model.get_relation("legend-db", rel_id)
        mongo_creds_json = rel.data[event.app].get("legend-db-connection")
        if not mongo_creds_json:
            self.unit.status = model.WaitingStatus(
                "Awaiting DB relation data.")
            event.defer()
            return
        logger.debug(
            "Mongo JSON credentials returned by DB relation: %s",
            mongo_creds_json)

        mongo_creds = None
        try:
            mongo_creds = json.loads(mongo_creds_json)
        except (ValueError, TypeError) as ex:
            logger.warn(
                "Exception occured while deserializing DB relation "
                "connection data: %s", str(ex))
            self.unit.status = model.BlockedStatus(
                "Could not deserialize Legend DB connection data.")
            return
        logger.debug(
            "Deserialized Mongo credentials returned by DB relation: %s",
            mongo_creds)

        self._stored.mongodb_credentials = mongo_creds

        # Attempt to reconfigure and restart the service with the new data:
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
                "Waiting for SDLC relation to report service URL.")
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
                "Waiting for Engine relation to report service URL.")
            return

        logger.info("### Engine URL received from relation: %s", engine_url)
        self._stored.engine_service_url = engine_url

        # Attempt to reconfigure and restart the service with the new data:
        self._reconfigure_studio_service()


if __name__ == "__main__":
    main.main(LegendStudioServerOperatorCharm)
