from django.db import models, transaction
from django.db.models.signals import post_save
from actstream import action
from django.dispatch import receiver
from django.contrib.auth.models import UserManager, User, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.conf import settings
from polymorphic.models import PolymorphicModel, PolymorphicManager
from django.core.exceptions import ValidationError
from policyengine.views import execute_policy
from datetime import datetime, timezone
import urllib
import json
import logging

logger = logging.getLogger(__name__)

# Default values for code fields in editor
DEFAULT_FILTER = "return True\n\n"
DEFAULT_INITIALIZE = "pass\n\n"
DEFAULT_CHECK = "return PASSED\n\n"
DEFAULT_NOTIFY = "pass\n\n"
DEFAULT_SUCCESS = "action.execute()\n\n"
DEFAULT_FAIL = "pass\n\n"

def on_transaction_commit(func):
    def inner(*args, **kwargs):
        transaction.on_commit(lambda: func(*args, **kwargs))
    return inner

class StarterKit(PolymorphicModel):
    name = models.TextField(null=True, blank=True, default = '')
    platform = models.TextField(null=True, blank=True, default = '')

    def __str__(self):
        return self.name


class CommunityManager(PolymorphicManager):
    def get_by_metagov_name(self, name):
        """
        Iterate through all communities to find the one we're looking for. This is
        not performant, if there are a lot of communities we should add the metagov name
        as a CharField on Community.
        """
        from integrations.metagov.library import metagov_slug
        for community in self.get_queryset().all():
            if metagov_slug(community) == name:
                return community
        raise Community.DoesNotExist


class Community(PolymorphicModel):
    """Community"""

    community_name = models.CharField('team_name', max_length=1000)
    platform = None
    base_role = models.OneToOneField('CommunityRole', models.CASCADE, related_name='base_community')

    objects = CommunityManager()

    def __str__(self):
        return self.community_name

    def notify_action(self, action, policy, users):
        """
        Sends a notification to users of a pending action.

        Parameters
        -------
        action
            The pending action.
        policy
            The policy being proposed.
        users
            The users who should be notified.
        """
        pass

    def get_roles(self):
        """
        Returns a QuerySet of all roles in the community.
        """
        return CommunityRole.objects.filter(community=self)

    def get_platform_policies(self):
        """
        Returns a QuerySet of all platform policies in the community.
        """
        return PlatformPolicy.objects.filter(community=self)

    def get_constitution_policies(self):
        """
        Returns a QuerySet of all constitution policies in the community.
        """
        return ConstitutionPolicy.objects.filter(community=self)

    def get_documents(self):
        """
        Returns a QuerySet of all documents in the community.
        """
        return CommunityDoc.objects.filter(community=self)

    def save(self, *args, **kwargs):
        if not self.pk and settings.METAGOV_ENABLED:
            # Create a corresponding community in Metagov
            from integrations.metagov.library import update_metagov_community
            update_metagov_community(self)

        super(Community, self).save(*args, **kwargs)

class CommunityRole(Group):
    community = models.ForeignKey(Community, models.CASCADE, null=True)
    role_name = models.TextField('readable_name', max_length=300, null=True)
    description = models.TextField(null=True, blank=True, default='')

    class Meta:
        verbose_name = 'communityrole'
        verbose_name_plural = 'communityroles'

    def save(self, *args, **kwargs):
        super(CommunityRole, self).save(*args, **kwargs)

    def __str__(self):
        return str(self.role_name)

class PolymorphicUserManager(UserManager, PolymorphicManager):
    # no-op class to get rid of warnings (issue #270)
    pass

class CommunityUser(User, PolymorphicModel):
    readable_name = models.CharField('readable_name', max_length=300, null=True)
    community = models.ForeignKey(Community, models.CASCADE)
    access_token = models.CharField('access_token', max_length=300, null=True)
    is_community_admin = models.BooleanField(default=False)
    avatar = models.CharField('avatar', max_length=500, null=True)

    objects = PolymorphicUserManager()

    def __str__(self):
        return self.readable_name if self.readable_name else self.username

    def save(self, *args, **kwargs):
        super(CommunityUser, self).save(*args, **kwargs)
        self.community.base_role.user_set.add(self)

        # If this user is an admin in the community, give them access to edit the Metagov config
        if self.is_community_admin and settings.METAGOV_ENABLED:
            from integrations.metagov.models import MetagovConfig
            role_name = "Metagov Admin"
            group_name = f"{self.community.platform}: {self.community.community_name}: {role_name}"
            role,created = CommunityRole.objects.get_or_create(community=self.community, role_name=role_name, name=group_name)
            if created:
                content_type = ContentType.objects.get_for_model(MetagovConfig)
                role.permissions.set(Permission.objects.filter(content_type=content_type))

            role.user_set.add(self)

    def get_roles(self):
        user_roles = []
        roles = CommunityRole.objects.filter(community=self.community)
        for r in roles:
            for u in r.user_set.all():
                if u.communityuser.username == self.username:
                    user_roles.append(r)
        return user_roles

    def has_role(self, role_name):
        roles = CommunityRole.objects.filter(community=self.community, role_name=role_name)
        if roles.exists():
            r = roles[0]
            for u in r.user_set.all():
                if u.communityuser.username == self.username:
                    return True
        return False

class CommunityDoc(models.Model):
    name = models.TextField(null=True, blank=True, default = '')
    text = models.TextField(null=True, blank=True, default = '')
    community = models.ForeignKey(Community, models.CASCADE, null=True)

    def __str__(self):
        return str(self.name)

    def save(self, *args, **kwargs):
        super(CommunityDoc, self).save(*args, **kwargs)

class DataStore(models.Model):
    data_store = models.TextField()

    def _get_data_store(self):
        if self.data_store != '':
            return json.loads(self.data_store)
        else:
            return {}

    def _set_data_store(self, obj):
        self.data_store = json.dumps(obj)
        self.save()

    def get(self, key):
        obj = self._get_data_store()
        return obj.get(key, None)

    def set(self, key, value):
        obj = self._get_data_store()
        obj[key] = value
        self._set_data_store(obj)
        return True

    def remove(self, key):
        obj = self._get_data_store()
        res = obj.pop(key, None)
        self._set_data_store(obj)
        if not res:
            return False
        return True


class LogAPICall(models.Model):
    community = models.ForeignKey(Community, models.CASCADE)
    proposal_time = models.DateTimeField(auto_now_add=True)
    call_type = models.CharField('call_type', max_length=300)
    extra_info = models.TextField()

    @classmethod
    def make_api_call(cls, community, values, call, action=None, method=None):
        _ = LogAPICall.objects.create(community=community,
                                      call_type=call,
                                      extra_info=json.dumps(values)
                                      )
        res = community.make_call(call, values=values, action=action, method=method)
        return res

class GenericPolicy(models.Model):
    starterkit = models.ForeignKey(StarterKit, on_delete=models.CASCADE)
    name = models.TextField(null=True, blank=True, default = '')
    description = models.TextField(null=True, blank=True, default = '')
    filter = models.TextField(null=True, blank=True, default='')
    initialize = models.TextField(null=True, blank=True, default='')
    check = models.TextField(null=True, blank=True, default='')
    notify = models.TextField(null=True, blank=True, default='')
    success = models.TextField(null=True, blank=True, default='')
    fail = models.TextField(null=True, blank=True, default='')
    is_bundled = models.BooleanField(default=False)
    has_notified = models.BooleanField(default=False)
    is_constitution = models.BooleanField(default=True)

    def __str__(self):
        return self.name

class GenericRole(Group):
    starterkit = models.ForeignKey(StarterKit, on_delete=models.CASCADE)
    role_name = models.TextField(blank=True, null=True, default='')
    description = models.TextField(blank=True, null=True, default='')
    is_base_role = models.BooleanField(default=False)
    user_group = models.TextField(blank=True, null=True, default='')
    plat_perm_set = models.TextField(blank=True, null=True, default='')

    def __str__(self):
        return self.role_name

class Proposal(models.Model):
    PROPOSED = 'proposed'
    FAILED = 'failed'
    PASSED = 'passed'
    STATUS = [
        (PROPOSED, 'proposed'),
        (FAILED, 'failed'),
        (PASSED, 'passed')
    ]

    author = models.ForeignKey(
        CommunityUser,
        models.CASCADE,
        verbose_name='author',
        blank=True,
        null=True
    )
    proposal_time = models.DateTimeField(auto_now_add=True)
    status = models.CharField(choices=STATUS, max_length=10)

    def get_time_elapsed(self):
        return datetime.now(timezone.utc) - self.proposal_time

    def get_all_boolean_votes(self, users=None):
        if users:
            return BooleanVote.objects.filter(proposal=self, user__in=users)
        return BooleanVote.objects.filter(proposal=self)

    def get_yes_votes(self, users=None):
        if users:
            return BooleanVote.objects.filter(boolean_value=True, proposal=self, user__in=users)
        return BooleanVote.objects.filter(boolean_value=True, proposal=self)

    def get_no_votes(self, users=None):
        if users:
            return BooleanVote.objects.filter(boolean_value=False, proposal=self, user__in=users)
        return BooleanVote.objects.filter(boolean_value=False, proposal=self)

    def get_all_number_votes(self, users=None):
        if users:
            return NumberVote.objects.filter(proposal=self, user__in=users)
        return NumberVote.objects.filter(proposal=self)

    def get_one_number_votes(self, value, users=None):
        if users:
            return NumberVote.objects.filter(number_value=value, proposal=self, user__in=users)
        return NumberVote.objects.filter(number_value=value, proposal=self)

    def save(self, *args, **kwargs):
        if not self.pk:
            self.data = DataStore.objects.create()
        super(Proposal, self).save(*args, **kwargs)

class BaseAction(models.Model):
    community = models.ForeignKey(Community, models.CASCADE, verbose_name='community')
    community_post = models.CharField('community_post', max_length=300, null=True)
    proposal = models.OneToOneField(Proposal, models.CASCADE)
    is_bundled = models.BooleanField(default=False)

    app_name = 'policyengine'

    data = models.OneToOneField(DataStore,
        models.CASCADE,
        verbose_name='data',
        null=True
    )

    class Meta:
        abstract = True

class ConstitutionAction(BaseAction, PolymorphicModel):
    community = models.ForeignKey(Community, models.CASCADE)
    initiator = models.ForeignKey(CommunityUser, models.CASCADE, null=True)
    is_bundled = models.BooleanField(default=False)

    action_type = "ConstitutionAction"
    action_codename = ''

    class Meta:
        verbose_name = 'constitutionaction'
        verbose_name_plural = 'constitutionactions'

    def pass_action(self):
        self.proposal.status = Proposal.PASSED
        self.proposal.save()
        action.send(self, verb='was passed', community_id=self.community.id)

    def fail_action(self):
        self.proposal.status = Proposal.FAILED
        self.proposal.save()

    def shouldCreate(self):
        return not self.pk # Runs only when object is new

    def save(self, *args, **kwargs):
        if self.shouldCreate():
            if self.data is None:
                self.data = DataStore.objects.create()

            #runs only if they have propose permission
            if self.initiator.has_perm(self._meta.app_label + '.add_' + self.action_codename):
                if hasattr(self, 'proposal'):
                    self.proposal.status = Proposal.PROPOSED
                else:
                    self.proposal = Proposal.objects.create(status=Proposal.PROPOSED, author=self.initiator)
                super(ConstitutionAction, self).save(*args, **kwargs)

                if not self.is_bundled:
                    action = self
                    #if they have execute permission, skip all policies
                    if action.initiator.has_perm(action.app_name + '.can_execute_' + action.action_codename):
                        action.execute()
                    else:
                        for policy in ConstitutionPolicy.objects.filter(community=self.community):
                            execute_policy(policy, action, is_first_evaluation=True)
            else:
                self.proposal = Proposal.objects.create(status=Proposal.FAILED, author=self.initiator)
        else:
            if not self.pk: # Runs only when object is new
                self.proposal = Proposal.objects.create(status=Proposal.FAILED, author=self.initiator)
            super(ConstitutionAction, self).save(*args, **kwargs)


class ConstitutionActionBundle(BaseAction):
    ELECTION = 'election'
    BUNDLE = 'bundle'
    BUNDLE_TYPE = [
        (ELECTION, 'election'),
        (BUNDLE, 'bundle')
    ]

    action_type = "ConstitutionActionBundle"

    bundled_actions = models.ManyToManyField(ConstitutionAction)
    bundle_type = models.CharField(choices=BUNDLE_TYPE, max_length=10)

    def execute(self):
        if self.bundle_type == ConstitutionActionBundle.BUNDLE:
            for action in self.bundled_actions.all():
                action.execute()
                action.pass_action()

    def pass_action(self):
        proposal = self.proposal
        proposal.status = Proposal.PASSED
        proposal.save()

    def fail_action(self):
        proposal = self.proposal
        proposal.status = Proposal.FAILED
        proposal.save()

    class Meta:
        verbose_name = 'constitutionactionbundle'
        verbose_name_plural = 'constitutionactionbundles'

    def save(self, *args, **kwargs):
        if not self.pk:
            action = self
            if action.initiator.has_perm(action.app_name + '.add_' + action.action_codename):
                #if they have execute permission, skip all policies
                if action.initiator.has_perm(action.app_name + '.can_execute_' + action.action_codename):
                    action.execute()
                else:
                    for policy in ConstitutionPolicy.objects.filter(community=self.community):
                        execute_policy(policy, action, is_first_evaluation=True)

        super(ConstitutionActionBundle, self).save(*args, **kwargs)

class PolicykitAddCommunityDoc(ConstitutionAction):
    name = models.TextField()
    text = models.TextField()

    action_codename = 'policykitaddcommunitydoc'

    def __str__(self):
        return "Add Document: " + self.name

    def execute(self):
        doc, _ = CommunityDoc.objects.get_or_create(name=self.name, text=self.text)
        doc.community = self.community
        doc.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitaddcommunitydoc', 'Can execute policykit add community doc'),
        )

class PolicykitChangeCommunityDoc(ConstitutionAction):
    doc = models.ForeignKey(CommunityDoc, models.SET_NULL, null=True)
    name = models.TextField()
    text = models.TextField()

    action_codename = 'policykitchangecommunitydoc'

    def __str__(self):
        return "Edit Document: " + self.name

    def execute(self):
        self.doc.name = self.name
        self.doc.text = self.text
        self.doc.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitchangecommunitydoc', 'Can execute policykit change community doc'),
        )

class PolicykitDeleteCommunityDoc(ConstitutionAction):
    doc = models.ForeignKey(CommunityDoc, models.SET_NULL, null=True)

    action_codename = 'policykitdeletecommunitydoc'

    def __str__(self):
        if self.doc:
            return "Delete Document: " + self.doc.name
        return "Delete Document"

    def execute(self):
        self.doc.delete()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitdeletecommunitydoc', 'Can execute policykit delete community doc'),
        )

class PolicykitAddRole(ConstitutionAction):
    name = models.CharField('name', max_length=300)
    description = models.TextField(null=True, blank=True, default='')
    permissions = models.ManyToManyField(Permission)

    action_codename = 'policykitaddrole'
    ready = False

    def __str__(self):
        return "Add Role: " + self.name

    def shouldCreate(self):
        return self.ready

    def execute(self):
        role, _ = CommunityRole.objects.get_or_create(
            role_name=self.name,
            name=self.community.platform + ": " + self.community.community_name + ": " + self.name,
            description=self.description
        )
        for p in self.permissions.all():
            role.permissions.add(p)
        role.community = self.community
        role.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitaddrole', 'Can execute policykit add role'),
        )

class PolicykitDeleteRole(ConstitutionAction):
    role = models.ForeignKey(CommunityRole, models.SET_NULL, null=True)

    action_codename = 'policykitdeleterole'

    def __str__(self):
        if self.role:
            return "Delete Role: " + self.role.role_name
        else:
            return "Delete Role: [ERROR: role not found]"

    def execute(self):
        try:
            self.role.delete()
        except AssertionError: # Triggers if object has already been deleted
            pass
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitdeleterole', 'Can execute policykit delete role'),
        )

class PolicykitEditRole(ConstitutionAction):
    role = models.ForeignKey(CommunityRole, models.SET_NULL, null=True)
    name = models.CharField('name', max_length=300)
    description = models.TextField(null=True, blank=True, default='')
    permissions = models.ManyToManyField(Permission)

    action_codename = 'policykiteditrole'
    ready = False

    def __str__(self):
        return "Edit Role: " + self.name

    def shouldCreate(self):
        return self.ready

    def execute(self):
        self.role.role_name = self.name
        self.role.description = self.description
        self.role.permissions.clear()
        for p in self.permissions.all():
            self.role.permissions.add(p)
        self.role.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykiteditrole', 'Can execute policykit edit role'),
        )

class PolicykitAddUserRole(ConstitutionAction):
    role = models.ForeignKey(CommunityRole, models.CASCADE)
    users = models.ManyToManyField(CommunityUser)

    action_codename = 'policykitadduserrole'
    ready = False

    def __str__(self):
        if self.role:
            return "Add User: " + str(self.users.all()[0]) + " to Role: " + self.role.role_name
        else:
            return "Add User to Role: [ERROR: role not found]"

    def shouldCreate(self):
        return self.ready

    def execute(self):
        for u in self.users.all():
            self.role.user_set.add(u)
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitadduserrole', 'Can execute policykit add user role'),
        )

class PolicykitRemoveUserRole(ConstitutionAction):
    role = models.ForeignKey(CommunityRole, models.CASCADE)
    users = models.ManyToManyField(CommunityUser)

    action_codename = 'policykitremoveuserrole'
    ready = False

    def __str__(self):
        if self.role:
            return "Remove User: " + str(self.users.all()[0]) + " from Role: " + self.role.role_name
        else:
            return "Remove User from Role: [ERROR: role not found]"

    def shouldCreate(self):
        return self.ready

    def execute(self):
        for u in self.users.all():
            self.role.user_set.remove(u)
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitremoveuserrole', 'Can execute policykit remove user role'),
        )

class EditorModel(ConstitutionAction):
    name = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    filter = models.TextField(blank=True,
        default=DEFAULT_FILTER,
        verbose_name="Filter"
    )
    initialize = models.TextField(blank=True,
        default=DEFAULT_INITIALIZE,
        verbose_name="Initialize"
    )
    check = models.TextField(blank=True,
        default=DEFAULT_CHECK,
        verbose_name="Check"
    )
    notify = models.TextField(blank=True,
        default=DEFAULT_NOTIFY,
        verbose_name="Notify"
    )
    success = models.TextField(blank=True,
        default=DEFAULT_SUCCESS,
        verbose_name="Pass"
    )
    fail = models.TextField(blank=True,
        default=DEFAULT_FAIL,
        verbose_name="Fail"
    )

class PolicykitAddPlatformPolicy(EditorModel):
    action_codename = 'policykitaddplatformpolicy'

    def __str__(self):
        return "Add Platform Policy: " + self.name

    def execute(self):
        policy = PlatformPolicy()
        policy.name = self.name
        policy.description = self.description
        policy.is_bundled = self.is_bundled
        policy.filter = self.filter
        policy.initialize = self.initialize
        policy.check = self.check
        policy.notify = self.notify
        policy.success = self.success
        policy.fail = self.fail
        policy.community = self.community
        policy.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_addpolicykitplatformpolicy', 'Can execute policykit add platform policy'),
        )

class PolicykitAddConstitutionPolicy(EditorModel):
    action_codename = 'policykitaddconstitutionpolicy'

    def __str__(self):
        return "Add Constitution Policy: " + self.name

    def execute(self):
        policy = ConstitutionPolicy()
        policy.community = self.community
        policy.name = self.name
        policy.description = self.description
        policy.is_bundled = self.is_bundled
        policy.filter = self.filter
        policy.initialize = self.initialize
        policy.check = self.check
        policy.notify = self.notify
        policy.success = self.success
        policy.fail = self.fail
        policy.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitaddconstitutionpolicy', 'Can execute policykit add constitution policy'),
        )

class PolicykitChangePlatformPolicy(EditorModel):
    platform_policy = models.ForeignKey('PlatformPolicy', models.CASCADE)

    action_codename = 'policykitchangeplatformpolicy'

    def __str__(self):
        return "Change Platform Policy: " + self.name

    def execute(self):
        self.platform_policy.name = self.name
        self.platform_policy.description = self.description
        self.platform_policy.filter = self.filter
        self.platform_policy.initialize = self.initialize
        self.platform_policy.check = self.check
        self.platform_policy.notify = self.notify
        self.platform_policy.success = self.success
        self.platform_policy.fail = self.fail
        self.platform_policy.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitchangeplatformpolicy', 'Can execute policykit change platform policy'),
        )

class PolicykitChangeConstitutionPolicy(EditorModel):
    constitution_policy = models.ForeignKey('ConstitutionPolicy', models.CASCADE)

    action_codename = 'policykitchangeconstitutionpolicy'

    def __str__(self):
        return "Change Constitution Policy: " + self.name

    def execute(self):
        self.constitution_policy.name = self.name
        self.constitution_policy.description = self.description
        self.constitution_policy.filter = self.filter
        self.constitution_policy.initialize = self.initialize
        self.constitution_policy.check = self.check
        self.constitution_policy.notify = self.notify
        self.constitution_policy.success = self.success
        self.constitution_policy.fail = self.fail
        self.constitution_policy.save()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitchangeconstitutionpolicy', 'Can execute policykit change constitution policy'),
        )

class PolicykitRemovePlatformPolicy(ConstitutionAction):
    platform_policy = models.ForeignKey('PlatformPolicy',
                                         models.SET_NULL,
                                         null=True)

    action_codename = 'policykitremoveplatformpolicy'

    def __str__(self):
        if self.platform_policy:
            return "Remove Platform Policy: " + self.platform_policy.name
        return "Remove Platform Policy: [ERROR: platform policy not found]"

    def execute(self):
        self.platform_policy.delete()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitremoveplatformpolicy', 'Can execute policykit remove platform policy'),
        )

class PolicykitRemoveConstitutionPolicy(ConstitutionAction):
    constitution_policy = models.ForeignKey('ConstitutionPolicy',
                                            models.SET_NULL,
                                            null=True)

    action_codename = 'policykitremoveconstitutionpolicy'

    def __str__(self):
        if self.constitution_policy:
            return "Remove Constitution Policy: " + self.constitution_policy.name
        return "Remove Constitution Policy: [ERROR: constitution policy not found]"

    def execute(self):
        self.constitution_policy.delete()
        self.pass_action()

    class Meta:
        permissions = (
            ('can_execute_policykitremoveconstitutionpolicy', 'Can execute policykit remove constitution policy'),
        )

class PlatformAction(BaseAction,PolymorphicModel):
    ACTION = None
    AUTH = 'app'

    community = models.ForeignKey(Community, models.CASCADE)
    initiator = models.ForeignKey(CommunityUser, models.CASCADE)
    community_revert = models.BooleanField(default=False)
    community_origin = models.BooleanField(default=False)
    is_bundled = models.BooleanField(default=False)

    action_type = "PlatformAction"
    action_codename = ''

    class Meta:
        verbose_name = 'platformaction'
        verbose_name_plural = 'platformactions'

    def revert(self, values, call, method=None):
        _ = LogAPICall.make_api_call(self.community, values, call, method=method)
        self.community_revert = True
        self.save()

    def execute(self):
        self.community.execute_platform_action(self)
        self.pass_action()

    def pass_action(self):
        self.proposal.status = Proposal.PASSED
        self.proposal.save()
        action.send(self, verb='was passed', community_id=self.community.id)

    def fail_action(self):
        self.proposal.status = Proposal.FAILED
        self.proposal.save()

    def save(self, *args, **kwargs):
        if not self.pk:
            if self.data is None:
                self.data = DataStore.objects.create()

            #runs only if they have propose permission
            if self.initiator.has_perm(self._meta.app_label + '.add_' + self.action_codename):
                self.proposal = Proposal.objects.create(status=Proposal.PROPOSED,
                                                author=self.initiator)

                super(PlatformAction, self).save(*args, **kwargs)

                if not self.is_bundled:
                    action = self
                    #if they have execute permission, skip all policies
                    if action.initiator.has_perm(action.app_name + '.can_execute_' + action.action_codename):
                        action.execute()
                    else:
                        for policy in PlatformPolicy.objects.filter(community=self.community):
                            execute_policy(policy, action, is_first_evaluation=True)
            else:
                self.proposal = Proposal.objects.create(status=Proposal.FAILED,
                                                        author=self.initiator)
                super(PlatformAction, self).save(*args, **kwargs)
        else:
            super(PlatformAction, self).save(*args, **kwargs)

class PlatformActionBundle(BaseAction):
    ELECTION = 'election'
    BUNDLE = 'bundle'
    BUNDLE_TYPE = [
        (ELECTION, 'election'),
        (BUNDLE, 'bundle')
    ]

    action_type = "PlatformActionBundle"
    bundled_actions = models.ManyToManyField(PlatformAction)
    bundle_type = models.CharField(choices=BUNDLE_TYPE, max_length=10)

    def execute(self):
        if self.bundle_type == PlatformActionBundle.BUNDLE:
            for action in self.bundled_actions.all():
                self.community.execute_platform_action(action)
                action.pass_action()

    def pass_action(self):
        proposal = self.proposal
        proposal.status = Proposal.PASSED
        proposal.save()

    def fail_action(self):
        proposal = self.proposal
        proposal.status = Proposal.FAILED
        proposal.save()

    class Meta:
        verbose_name = 'platformactionbundle'
        verbose_name_plural = 'platformactionbundles'

    def save(self, *args, **kwargs):
        if not self.pk:
            action = self
            if action.initiator.has_perm(action.app_name + '.add_' + action.action_codename):
                #if they have execute permission, skip all policies
                if action.initiator.has_perm(action.app_name + '.can_execute_' + action.action_codename):
                    action.execute()
                elif not action.community_post:
                    for policy in PlatformPolicy.objects.filter(community=action.community):
                        execute_policy(policy, action, is_first_evaluation=True)

        super(PlatformActionBundle, self).save(*args, **kwargs)


class BasePolicy(models.Model):
    filter = models.TextField(blank=True, default='')
    initialize = models.TextField(blank=True, default='')
    check = models.TextField(blank=True, default='')
    notify = models.TextField(blank=True, default='')
    success = models.TextField(blank=True, default='')
    fail = models.TextField(blank=True, default='')

    name = models.TextField(null=True, blank=True)
    community = models.ForeignKey(Community,
        models.CASCADE,
        verbose_name='community',
    )
    description = models.TextField(null=True, blank=True)
    is_bundled = models.BooleanField(default=False)
    has_notified = models.BooleanField(default=False)

    data = models.OneToOneField(DataStore,
        models.CASCADE,
        verbose_name='data',
        null=True
    )

    class Meta:
        abstract = True

class ConstitutionPolicy(BasePolicy):
    policy_type = "ConstitutionPolicy"

    class Meta:
        verbose_name = 'constitutionpolicy'
        verbose_name_plural = 'constitutionpolicies'

    def __str__(self):
        return 'ConstitutionPolicy: ' + self.name

class ConstitutionPolicyBundle(BasePolicy):
    bundled_policies = models.ManyToManyField(ConstitutionPolicy)
    policy_type = "ConstitutionPolicyBundle"

    class Meta:
        verbose_name = 'constitutionpolicybundle'
        verbose_name_plural = 'constitutionpolicybundles'

class PlatformPolicy(BasePolicy):
    policy_type = "PlatformPolicy"

    class Meta:
        verbose_name = 'platformpolicy'
        verbose_name_plural = 'platformpolicies'

    def __str__(self):
        return 'PlatformPolicy: ' + self.name

class PlatformPolicyBundle(BasePolicy):
    bundled_policies = models.ManyToManyField(PlatformPolicy)
    policy_type = "PlatformPolicyBundle"

    class Meta:
        verbose_name = 'platformpolicybundle'
        verbose_name_plural = 'platformpolicybundles'

class UserVote(models.Model):
    user = models.ForeignKey(CommunityUser, models.CASCADE)
    proposal = models.ForeignKey(Proposal, models.CASCADE)
    vote_time = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def get_time_elapsed(self):
        return datetime.now(timezone.utc) - self.vote_time

class BooleanVote(UserVote):
    TRUE_FALSE_CHOICES = (
        (True, 'Yes'),
        (False, 'No')
    )
    boolean_value = models.BooleanField(
        null=True,
        choices=TRUE_FALSE_CHOICES,
        default=True
    )

    def __str__(self):
        return str(self.user) + ' : ' + str(self.boolean_value)

class NumberVote(UserVote):
    number_value = models.IntegerField(null=True)

    def __str__(self):
        return str(self.user) + ' : ' + str(self.number_value)
