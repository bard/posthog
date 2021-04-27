import os
from typing import Any, Dict

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch.dispatcher import receiver
from rest_framework.exceptions import ValidationError
from semantic_version.base import SimpleSpec, Version

from posthog.models.organization import Organization
from posthog.models.team import Team
from posthog.plugins.reload import reload_plugins_on_workers
from posthog.plugins.utils import download_plugin_archive, get_json_from_archive, load_json_file, parse_url
from posthog.version import VERSION


def raise_if_plugin_installed(url: str, organization_id: str):
    url_without_private_key = url.split("?")[0]
    if (
        Plugin.objects.filter(
            models.Q(url=url_without_private_key) | models.Q(url__startswith="{}?".format(url_without_private_key))
        )
        .filter(organization_id=organization_id)
        .exists()
    ):
        raise ValidationError('Plugin from URL "{}" already installed!'.format(url_without_private_key))


def update_validated_data_from_url(validated_data: Dict[str, Any], url: str) -> Dict:
    """If remote plugin, download the archive and get up-to-date validated_data from there."""
    if url.startswith("file:"):
        plugin_path = url[5:]
        json_path = os.path.join(plugin_path, "plugin.json")
        json = load_json_file(json_path)
        if not json:
            raise ValidationError("Could not load plugin.json from: {}".format(json_path))
        validated_data["plugin_type"] = "local"
        validated_data["url"] = url
        validated_data["tag"] = None
        validated_data["archive"] = None
        validated_data["name"] = json.get("name", json_path.split("/")[-2])
        validated_data["description"] = json.get("description", "")
        validated_data["config_schema"] = json.get("config", [])
        validated_data["source"] = None
        posthog_version = json.get("posthogVersion", None)
    else:
        parsed_url = parse_url(url, get_latest_if_none=True)
        if parsed_url:
            validated_data["url"] = parsed_url["root_url"]
            validated_data["tag"] = parsed_url.get("tag", None)
            validated_data["archive"] = download_plugin_archive(validated_data["url"], validated_data["tag"])
            plugin_json = get_json_from_archive(validated_data["archive"], "plugin.json")
            if not plugin_json:
                raise ValidationError("Could not find plugin.json in the plugin")
            validated_data["name"] = plugin_json["name"]
            validated_data["description"] = plugin_json.get("description", "")
            validated_data["config_schema"] = plugin_json.get("config", [])
            validated_data["source"] = None
            posthog_version = plugin_json.get("posthogVersion", None)
        else:
            raise ValidationError("Must be a GitHub/GitLab repository or a npm package URL!")

        # Keep plugin type as "repository" or reset to "custom" if it was something else.
        if (
            validated_data.get("plugin_type", None) != Plugin.PluginType.CUSTOM
            and validated_data.get("plugin_type", None) != Plugin.PluginType.REPOSITORY
        ):
            validated_data["plugin_type"] = Plugin.PluginType.CUSTOM

    if posthog_version and not settings.MULTI_TENANCY:
        try:
            spec = SimpleSpec(posthog_version.replace(" ", ""))
        except ValueError:
            raise ValidationError(f'Invalid PostHog semantic version requirement "{posthog_version}"!')
        if not (Version(VERSION) in spec):
            raise ValidationError(
                f'Currently running PostHog version {VERSION} does not match this plugin\'s semantic version requirement "{posthog_version}".'
            )

    return validated_data


class PluginManager(models.Manager):
    def install(self, **kwargs) -> "Plugin":
        if "organization_id" not in kwargs and "organization" in kwargs:
            kwargs["organization_id"] = kwargs["organization"].id
        if kwargs.get("plugin_type", None) != Plugin.PluginType.SOURCE:
            update_validated_data_from_url(kwargs, kwargs["url"])
            raise_if_plugin_installed(kwargs["url"], kwargs["organization_id"])
        reload_plugins_on_workers()
        return Plugin.objects.create(**kwargs)


class Plugin(models.Model):
    class PluginType(models.TextChoices):
        LOCAL = "local", "local"  # url starts with "file:"
        CUSTOM = "custom", "custom"  # github or npm url downloaded as zip or tar.gz into field "archive"
        REPOSITORY = "repository", "repository"  # same, but originating from our plugins.json repository
        SOURCE = "source", "source"  # coded inside the browser (versioned via plugin_source_version)

    organization: models.ForeignKey = models.ForeignKey(
        "posthog.Organization", on_delete=models.CASCADE, related_name="plugins", related_query_name="plugin"
    )
    plugin_type: models.CharField = models.CharField(
        max_length=200, null=True, blank=True, choices=PluginType.choices, default=None
    )
    is_global: models.BooleanField = models.BooleanField(default=False)  # Whether plugin is installed for all orgs
    name: models.CharField = models.CharField(max_length=200, null=True, blank=True)
    description: models.TextField = models.TextField(null=True, blank=True)
    url: models.CharField = models.CharField(max_length=800, null=True, blank=True)
    # Describe the fields to ask in the interface; store answers in PluginConfig->config
    # - config_schema = { [fieldKey]: { name: 'api key', type: 'string', default: '', required: true }  }
    config_schema: models.JSONField = models.JSONField(default=dict)
    tag: models.CharField = models.CharField(max_length=200, null=True, blank=True)
    archive: models.BinaryField = models.BinaryField(blank=True, null=True)
    source: models.TextField = models.TextField(blank=True, null=True)
    latest_tag: models.CharField = models.CharField(max_length=800, null=True, blank=True)
    latest_tag_checked_at: models.DateTimeField = models.DateTimeField(null=True, blank=True)
    preinstalled: models.BooleanField = models.BooleanField(default=False)

    # DEPRECATED: not used for anything, all install and config errors are in PluginConfig.error
    error: models.JSONField = models.JSONField(default=None, null=True)
    # DEPRECATED: these were used when syncing posthog.json with the db on app start
    from_json: models.BooleanField = models.BooleanField(default=False)
    from_web: models.BooleanField = models.BooleanField(default=False)

    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    objects: PluginManager = PluginManager()


class PluginConfig(models.Model):
    team: models.ForeignKey = models.ForeignKey("Team", on_delete=models.CASCADE, null=True)
    plugin: models.ForeignKey = models.ForeignKey("Plugin", on_delete=models.CASCADE)
    enabled: models.BooleanField = models.BooleanField(default=False)
    order: models.IntegerField = models.IntegerField()
    config: models.JSONField = models.JSONField(default=dict)
    # Error when running this plugin on an event (frontend: PluginErrorType)
    # - e.g: "undefined is not a function on index.js line 23"
    # - error = { message: "Exception in processEvent()", time: "iso-string", ...meta }
    error: models.JSONField = models.JSONField(default=None, null=True)

    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)


class PluginAttachment(models.Model):
    team: models.ForeignKey = models.ForeignKey("Team", on_delete=models.CASCADE, null=True)
    plugin_config: models.ForeignKey = models.ForeignKey("PluginConfig", on_delete=models.CASCADE, null=True)
    key: models.CharField = models.CharField(max_length=200)
    content_type: models.CharField = models.CharField(max_length=200)
    file_name: models.CharField = models.CharField(max_length=200)
    file_size: models.IntegerField = models.IntegerField()
    contents: models.BinaryField = models.BinaryField()


class PluginStorage(models.Model):
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plugin_config_id", "key"], name="posthog_unique_plugin_storage_key")
        ]

    plugin_config: models.ForeignKey = models.ForeignKey("PluginConfig", on_delete=models.CASCADE)
    key: models.CharField = models.CharField(max_length=200)
    value: models.TextField = models.TextField(blank=True, null=True)


@receiver(models.signals.post_save, sender=Organization)
def preinstall_plugins_for_new_organization(sender, instance: Organization, created: bool, **kwargs):
    if created and not settings.TEST:  # Disable in tests to avoid hitting GitHub API limits
        for plugin_url in settings.PLUGINS_PREINSTALLED_URLS:
            Plugin.objects.install(
                organization=instance, plugin_type=Plugin.PluginType.REPOSITORY, url=plugin_url, preinstalled=True
            )


@receiver(models.signals.post_save, sender=Team)
def enable_preinstalled_plugins_for_new_team(sender, instance: Team, created: bool, **kwargs):
    if created:
        for order, preinstalled_plugin in enumerate(Plugin.objects.filter(preinstalled=True)):
            PluginConfig.objects.create(team=instance, plugin=preinstalled_plugin, enabled=True, order=order)


@receiver([post_save, post_delete], sender=Plugin)
def plugin_reload_needed(sender, instance, created=None, **kwargs):
    if not created:
        # Newly created plugins don't have a config yet, so no need to reload
        reload_plugins_on_workers()


@receiver([post_save, post_delete], sender=PluginConfig)
def plugin_config_reload_needed(sender, instance, created=None, **kwargs):
    reload_plugins_on_workers()


@receiver([post_save, post_delete], sender=PluginAttachment)
def plugin_attachement_reload_needed(sender, instance, created=None, **kwargs):
    reload_plugins_on_workers()
