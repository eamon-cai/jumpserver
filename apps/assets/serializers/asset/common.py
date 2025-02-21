# -*- coding: utf-8 -*-
#

from django.db.models import F
from django.db.transaction import atomic
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from accounts.models import Account
from accounts.serializers import AccountSerializerCreateValidateMixin
from accounts.serializers import AuthValidateMixin
from common.serializers import WritableNestedModelSerializer, SecretReadableMixin, CommonModelSerializer
from common.serializers.fields import LabeledChoiceField
from orgs.mixins.serializers import BulkOrgResourceModelSerializer
from ...const import Category, AllTypes
from ...models import Asset, Node, Platform, Label, Protocol

__all__ = [
    'AssetSerializer', 'AssetSimpleSerializer', 'MiniAssetSerializer',
    'AssetTaskSerializer', 'AssetsTaskSerializer', 'AssetProtocolsSerializer',
    'AssetDetailSerializer', 'DetailMixin', 'AssetAccountSerializer',
    'AccountSecretSerializer', 'SpecSerializer'
]


class AssetProtocolsSerializer(serializers.ModelSerializer):
    port = serializers.IntegerField(required=False, allow_null=True, max_value=65535, min_value=1)

    class Meta:
        model = Protocol
        fields = ['name', 'port']


class AssetLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = ['id', 'name', 'value']
        extra_kwargs = {
            # 取消默认唯一键的校验
            'id': {'validators': []},
            'name': {'required': False},
            'value': {'required': False},
        }


class AssetPlatformSerializer(serializers.ModelSerializer):
    class Meta:
        model = Platform
        fields = ['id', 'name']
        extra_kwargs = {
            'name': {'required': False}
        }


class AssetAccountSerializer(
    AuthValidateMixin,
    AccountSerializerCreateValidateMixin,
    CommonModelSerializer
):
    add_org_fields = False
    push_now = serializers.BooleanField(
        default=False, label=_("Push now"), write_only=True
    )
    template = serializers.BooleanField(
        default=False, label=_("Template"), write_only=True
    )
    name = serializers.CharField(max_length=128, required=True, label=_("Name"))

    class Meta:
        model = Account
        fields_mini = [
            'id', 'name', 'username', 'privileged',
            'is_active', 'version', 'secret_type',
        ]
        fields_write_only = [
            'secret', 'push_now', 'template'
        ]
        fields = fields_mini + fields_write_only
        extra_kwargs = {
            'secret': {'write_only': True},
        }

    def validate_push_now(self, value):
        request = self.context['request']
        if not request.user.has_perms('accounts.push_account'):
            return False
        return value

    def validate_name(self, value):
        if not value:
            value = self.initial_data.get('username')
        return value


class AccountSecretSerializer(SecretReadableMixin, CommonModelSerializer):
    class Meta:
        model = Account
        fields = [
            'name', 'username', 'privileged', 'secret_type', 'secret',
        ]
        extra_kwargs = {
            'secret': {'write_only': False},
        }


class SpecSerializer(serializers.Serializer):
    # 数据库
    db_name = serializers.CharField(label=_("Database"), max_length=128, required=False)
    use_ssl = serializers.BooleanField(label=_("Use SSL"), required=False)
    allow_invalid_cert = serializers.BooleanField(label=_("Allow invalid cert"), required=False)
    # Web
    autofill = serializers.CharField(label=_("Auto fill"), required=False)
    username_selector = serializers.CharField(label=_("Username selector"), required=False)
    password_selector = serializers.CharField(label=_("Password selector"), required=False)
    submit_selector = serializers.CharField(label=_("Submit selector"), required=False)
    script = serializers.JSONField(label=_("Script"), required=False)


class AssetSerializer(BulkOrgResourceModelSerializer, WritableNestedModelSerializer):
    category = LabeledChoiceField(choices=Category.choices, read_only=True, label=_('Category'))
    type = LabeledChoiceField(choices=AllTypes.choices(), read_only=True, label=_('Type'))
    labels = AssetLabelSerializer(many=True, required=False, label=_('Label'))
    protocols = AssetProtocolsSerializer(many=True, required=False, label=_('Protocols'), default=())
    accounts = AssetAccountSerializer(many=True, required=False, write_only=True, label=_('Account'))

    class Meta:
        model = Asset
        fields_mini = ['id', 'name', 'address']
        fields_small = fields_mini + ['is_active', 'comment']
        fields_fk = ['domain', 'platform']
        fields_m2m = [
            'nodes', 'labels', 'protocols',
            'nodes_display', 'accounts'
        ]
        read_only_fields = [
            'category', 'type', 'connectivity',
            'date_verified', 'created_by', 'date_created',
            'auto_info',
        ]
        fields = fields_small + fields_fk + fields_m2m + read_only_fields
        extra_kwargs = {
            'auto_info': {'label': _('Auto info')},
            'name': {'label': _("Name")},
            'address': {'label': _('Address')},
            'nodes_display': {'label': _('Node path')},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_field_choices()

    def _get_protocols_required_default(self):
        platform = self._initial_data_platform
        platform_protocols = platform.protocols.all()
        protocols_default = [p for p in platform_protocols if p.default]
        protocols_required = [p for p in platform_protocols if p.required or p.primary]
        return protocols_required, protocols_default

    def _set_protocols_default(self):
        if not hasattr(self, 'initial_data'):
            return
        protocols = self.initial_data.get('protocols')
        if protocols is not None:
            return

        protocols_required, protocols_default = self._get_protocols_required_default()
        protocols_data = [
            {'name': p.name, 'port': p.port}
            for p in protocols_required + protocols_default
        ]
        self.initial_data['protocols'] = protocols_data

    def _init_field_choices(self):
        request = self.context.get('request')
        if not request:
            return
        category = request.path.strip('/').split('/')[-1].rstrip('s')
        field_category = self.fields.get('category')
        field_category.choices = Category.filter_choices(category)
        field_type = self.fields.get('type')
        field_type.choices = AllTypes.filter_choices(category)

    @classmethod
    def setup_eager_loading(cls, queryset):
        """ Perform necessary eager loading of data. """
        queryset = queryset.prefetch_related('domain', 'nodes', 'labels', 'protocols') \
            .prefetch_related('platform', 'platform__automation') \
            .annotate(category=F("platform__category")) \
            .annotate(type=F("platform__type"))
        return queryset

    @staticmethod
    def perform_nodes_display_create(instance, nodes_display):
        if not nodes_display:
            return
        nodes_to_set = []
        for full_value in nodes_display:
            if not full_value.startswith('/'):
                full_value = '/' + instance.org.name + '/' + full_value
            node = Node.objects.filter(full_value=full_value).first()
            if node:
                nodes_to_set.append(node)
            else:
                node = Node.create_node_by_full_value(full_value)
            nodes_to_set.append(node)
        instance.nodes.set(nodes_to_set)

    @property
    def _initial_data_platform(self):
        if self.instance:
            return self.instance.platform

        platform_id = self.initial_data.get('platform')
        if isinstance(platform_id, dict):
            platform_id = platform_id.get('id') or platform_id.get('pk')
        platform = Platform.objects.filter(id=platform_id).first()
        if not platform:
            raise serializers.ValidationError({'platform': _("Platform not exist")})
        return platform

    def validate_domain(self, value):
        platform = self._initial_data_platform
        if platform.domain_enabled:
            return value
        else:
            return None

    def validate_nodes(self, nodes):
        if nodes:
            return nodes
        nodes_display = self.initial_data.get('nodes_display')
        if nodes_display:
            return nodes
        request = self.context.get('request')
        if not request:
            return []
        node_id = request.query_params.get('node_id')
        if not node_id:
            return []
        nodes = Node.objects.filter(id=node_id)
        return nodes

    def is_valid(self, raise_exception=False):
        self._set_protocols_default()
        return super().is_valid(raise_exception)

    def validate_protocols(self, protocols_data):
        # 目的是去重
        protocols_data_map = {p['name']: p for p in protocols_data}
        for p in protocols_data:
            port = p.get('port', 0)
            if port < 1 or port > 65535:
                error = p.get('name') + ': ' + _("port out of range (1-65535)")
                raise serializers.ValidationError(error)

        protocols_required, protocols_default = self._get_protocols_required_default()
        protocols_not_found = [p.name for p in protocols_required if p.name not in protocols_data_map]
        if protocols_not_found:
            raise serializers.ValidationError({
                'protocols': _("Protocol is required: {}").format(', '.join(protocols_not_found))
            })
        return protocols_data_map.values()

    @staticmethod
    def accounts_create(accounts_data, asset):
        for data in accounts_data:
            data['asset'] = asset
            AssetAccountSerializer().create(data)

    @atomic
    def create(self, validated_data):
        nodes_display = validated_data.pop('nodes_display', '')
        accounts = validated_data.pop('accounts', [])
        instance = super().create(validated_data)
        self.accounts_create(accounts, instance)
        self.perform_nodes_display_create(instance, nodes_display)
        return instance

    @atomic
    def update(self, instance, validated_data):
        if not validated_data.get('accounts'):
            validated_data.pop('accounts', None)
        nodes_display = validated_data.pop('nodes_display', '')
        instance = super().update(instance, validated_data)
        self.perform_nodes_display_create(instance, nodes_display)
        return instance


class DetailMixin(serializers.Serializer):
    accounts = AssetAccountSerializer(many=True, required=False, label=_('Accounts'))
    spec_info = serializers.DictField(label=_('Spec info'), read_only=True)
    auto_info = serializers.DictField(read_only=True, label=_('Auto info'))

    def get_field_names(self, declared_fields, info):
        names = super().get_field_names(declared_fields, info)
        names.extend([
            'accounts', 'info', 'spec_info', 'auto_info'
        ])
        return names


class AssetDetailSerializer(DetailMixin, AssetSerializer):
    pass


class MiniAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = AssetSerializer.Meta.fields_mini


class AssetSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = [
            'id', 'name', 'address', 'port',
            'connectivity', 'date_verified'
        ]


class AssetsTaskSerializer(serializers.Serializer):
    ACTION_CHOICES = (
        ('refresh', 'refresh'),
        ('test', 'test'),
    )
    task = serializers.CharField(read_only=True)
    action = serializers.ChoiceField(choices=ACTION_CHOICES, write_only=True)
    assets = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, required=False, allow_empty=True, many=True
    )


class AssetTaskSerializer(AssetsTaskSerializer):
    ACTION_CHOICES = tuple(list(AssetsTaskSerializer.ACTION_CHOICES) + [
        ('push_system_user', 'push_system_user'),
        ('test_system_user', 'test_system_user')
    ])
    action = serializers.ChoiceField(choices=ACTION_CHOICES, write_only=True)
    asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, required=False, allow_empty=True, many=False
    )
    accounts = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects, required=False, allow_empty=True, many=True
    )
