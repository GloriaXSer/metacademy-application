import pdb
import string
import json
import random
import ast

# myapp/api.py
from tastypie import fields
from tastypie.resources import ModelResource
from tastypie.authorization import DjangoAuthorization
from tastypie.authentication import SessionAuthentication
from tastypie.exceptions import Unauthorized
from tastypie.exceptions import ImmediateHttpResponse
from django.core.exceptions import ObjectDoesNotExist

from apps.graph.models import Concept, Edge, Flag, Graph, GraphSettings, ConceptSettings
from apps.graph.models import ConceptResource as CResource # avoid name collision
from apps.user_management.models import Profile


class ModAndUserObjectsOnlyAuthorization(DjangoAuthorization):
    def update_list(self, object_list, bundle):
        # called when PUTing a list
        allowed = []
        for obj in object_list:
            if bundle.obj.editable_by(bundle.request.user):
                allowed.append(obj)
            # TODO verify that we're not changing ids
        return allowed

    def update_detail(self, object_list, bundle):

        # check if we're trying to change the id or create a new object using patch
        # TODO I couldn't find a better way to do this --CJR
        reqmeth = bundle.request.META["REQUEST_METHOD"]
        if  reqmeth == "PATCH" or reqmeth == "PUT":
            split_path = bundle.request.META["PATH_INFO"].split("/")
            split_path = [p for p in split_path if p]
            model_name = split_path[-2]
            model_patch_id = split_path[-1]
            # make sure we're not trying to change the id
            if model_name == bundle.obj._meta.model_name and model_patch_id != bundle.obj.id and bundle.obj.__class__.objects.filter(id=model_patch_id).exists():

                raise Unauthorized("cannot replace id")

        return bundle.obj.editable_by(bundle.request.user)

    def delete_list(self, object_list, bundle):
        raise Unauthorized("Sorry, no deletes yet. TODO")

    def delete_detail(self, object_list, bundle):
        raise Unauthorized("Sorry, no deletes yet. TODO")

class CustomReversionResource(ModelResource):
    """
    ModelResource that uses django reversions
    """
    def save(self, bundle, skip_errors=False, **kwargs):
        self.is_valid(bundle)

        if bundle.errors and not skip_errors:
            raise ImmediateHttpResponse(response=self.error_response(bundle.request, bundle.errors))

        # Check if they're authorized.
        if bundle.obj.pk:
            self.authorized_update_detail(self.get_object_list(bundle.request), bundle)
        else:
            self.authorized_create_detail(self.get_object_list(bundle.request), bundle)

        self.pre_save_hook(bundle)

        # Save FKs just in case.
        self.save_related(bundle)

        # Save the main object. # CJR TODO we can somehow check if we should save here (are we calling from an edge?)
        bundle.obj.save()
        bundle.objects_saved.add(self.create_identifier(bundle.obj))

        # Now pick up the M2M bits. (must occur after the main obj)
        m2m_bundle = self.hydrate_m2m(bundle)
        self.save_m2m(m2m_bundle)

        # per resource post-save hook
        bundle = self.post_save_hook(bundle)

        return bundle

    def pre_save_hook(self, bundle):
        """
        called after saving to db

        implement in subclass
        """
        return bundle

    def post_save_hook(self, bundle):
        """
        called after saving to db

        implement in subclass
        """
        return bundle

    def obj_create(self, bundle, **kwargs):
        return super(ModelResource, self).obj_create(bundle, **kwargs)

    def obj_update(self, bundle, **kwargs):
        return super(ModelResource, self).obj_update(bundle, **kwargs)

    def obj_get(self, bundle, **kwargs):
        return super(ModelResource, self).obj_get(bundle, **kwargs)


class FlagResource(ModelResource):

    class Meta:
        max_limit = 0
        fields = ("text",)
        include_resource_uri = False
        queryset = Flag.objects.all()
        resource_name = 'flag'
        authorization = ModAndUserObjectsOnlyAuthorization()


class ShellConceptResource(ModelResource):
    """
    A simple "shell" concept (id and tag) to avoid infinite recursion with CRUD requests
    """
    class Meta:
        max_limit = 0
        fields = ("id", "tag")
        queryset = Concept.objects.all()
        resource_name = 'concept'
        include_resource_uri = False
        authorization = ModAndUserObjectsOnlyAuthorization()

class ConceptResourceResource(ModelResource):
    concept = fields.ForeignKey(ShellConceptResource, "concept", full=True)

    class Meta:
        max_limit = 0
        queryset = CResource.objects.all()
        resource_name = 'conceptresource'
        authorization = ModAndUserObjectsOnlyAuthorization()
        always_return_data = True

    def dehydrate(self, bundle):
        # TODO why is this called > 1 times? and why doesn't this flag stop it?
        if not hasattr(self, "was_dehydrated"):
            self.was_dehydrated = True
        elif self.was_dehydrated:
            return bundle

        bundle.data['authors'] = ast.literal_eval(bundle.data['authors'])
        bundle.data['location'] = json.loads(bundle.data['location'])
        adeps = bundle.data["additional_dependencies"]

        if type(adeps) == unicode:
            adeps = ast.literal_eval(adeps)
        for dep in adeps:
            if dep.has_key("id"):
                dconcept = Concept.objects.get(id=dep["id"])
                dep["title"] = dconcept.title
                dep["tag"] = dconcept.tag
            elif dep.has_key("title"):
                try:
                    dconcept = Concept.objects.get(title=dep["title"])
                    dep["title"] = dconcept.title
                    dep["tag"] = dconcept.tag
                    dep["id"] = dconcept.id
                except ObjectDoesNotExist:
                    pass # TODO
        bundle.data["additional_dependencies"] = adeps

        return bundle

    def hydrate(self, bundle, **kwargs):
        """
        prep the resource data for the database
        """

        # TODO make sure we're not trying to change ids, and keep track of what we are changing

        resource = bundle.data
        # hack because hydrate can be called twice (https://github.com/toastdriven/django-tastypie/issues/390)
        if type(resource) != dict:
            return bundle

        # create new id if necessary
        if not resource["id"] or resource["id"][:4] == "-new":
            useid = ''
            while not useid or not len(CResource.objects.filter(id=useid)) == 0:
                useid = ''.join([random.choice(string.lowercase + string.digits) for i in range(8)])
            resource["id"] = useid

        # normalize year TODO should we only allow ints
        if resource.has_key("year"):
            try:
                resource["year"]  = int(resource["year"])
            except:
                resource["year"]  = None

        # FIXME this shouldn't exist here, or at least, it should check
        # that the id doesn't exist (for that 1 in 4.7x10^18 chance)
        if not resource.has_key("id"):
            resource["id"] = ''.join([random.choice(string.lowercase + string.digits) for i in range(8)])

        # TODO check for temporary concept ids OFFLINE
        bdl_type = type(resource['location'])
        # TODO parse string entry or expect parsed?
        if bdl_type == list:
            # assume json TODO add try/except block
            resource['location'] = json.dumps(resource['location'])

        adeps_type = type(resource["additional_dependencies"])
        if adeps_type == str:
            adeps = ast.literal_eval(resource["additional_dependencies"])
        elif adeps_type == list:
            adeps = resource["additional_dependencies"]
        else:
            raise Exception("unable to parse additional dependencies for concept " + bundle.data["title"])
        save_adeps = []
        # if adeps don't have ids, try to associate an id with it -- only save the title if absolutely necessary
        for dep in adeps:
            did = ""
            if dep.has_key("id"):
                did = dep["id"]
            elif dep.has_key("title"):
                # try to find its id using the title
                # TODO which concepts should we filter on? e.g. all concepts, only approved concepts, [probably best solution: approved or user concepts]
                tobjs = Concept.objects.filter(title=dep["title"])
                if len(tobjs):
                    # TODO what if title's are ambiguous
                    did = tobjs[0].id
                else:
                    # TODO if possible,
                    # search input graph for a match
                    pass
            else:
                raise Exception("additional resource dependency for concept " +  bundle.data["title"] + " does not have id or title specified")
            if did:
                save_adep = {"id": did}
            else:
                save_adep = {"title": dep["title"]}
            save_adeps.append(save_adep)

        return bundle


class ConceptResource(CustomReversionResource):
    """
    API for concepts, aka nodes
    """
    resources = fields.ToManyField(ConceptResourceResource, 'concept_resource', full = True)
    flags = fields.ManyToManyField(FlagResource, 'flags', full=True)

    def dehydrate(self, bundle):
        # find the set of prereqs
        deps = Edge.objects.filter(target=bundle.data["id"])
        bundle.data["dependencies"] = [{"id": dep.id, "source": dep.source, "reason": dep.reason} for dep in deps]
        return bundle

    def pre_save_hook(self, bundle):
        # save edges and remove from bundle
        for in_edge in bundle.data["dependencies"]:
            edge, created = Edge.objects.get_or_create(id=in_edge["id"])
            edge.source = in_edge["source"]
            edge.target = in_edge["target"]
            edge.reason = in_edge["reason"]
            edge.save()
        del bundle.data["dependencies"]
        return bundle

    def post_save_hook(self, bundle):
        # FIXME we're assuming a user is logged in
        csettings, csnew = ConceptSettings.objects.get_or_create(concept=bundle.obj)
        uprof, created = Profile.objects.get_or_create(pk=bundle.request.user.pk)
        csettings.editors.add(uprof)
        csettings.save()
        return bundle

    class Meta:
        """ ConceptResource Meta"""
        max_limit = 0
        queryset = Concept.objects.all()
        resource_name = 'concept'
        authorization = ModAndUserObjectsOnlyAuthorization()
        allowed_methods = ("get", "post", "put", "delete", "patch")
        always_return_data = True

    def alter_deserialized_list_data(self, request, data):
        for concept in data["objects"]:
            normalize_concept(concept)
        return data

    def alter_deserialized_detail_data(self, request, data):
        normalize_concept(data)
        return data

    def hydrate_flags(self, bundle):
        in_concept = bundle.data
        if in_concept["flags"]:
            flag_arr = []
            for flag in in_concept["flags"]:
                flag_arr.append({"text": flag})
            in_concept["flags"] = flag_arr
        return bundle

    def hydrate(self, bundle):
        in_concept = bundle.data
        for in_inlink in in_concept["dependencies"]:
            if type(in_inlink) != dict:
                # hack because hydrate can be called twice (https://github.com/toastdriven/django-tastypie/issues/390)
                continue
            if in_inlink.has_key('sid_source'):
                in_inlink['source'] = in_inlink['sid_source']
            if in_inlink.has_key('sid_target'):
                in_inlink['target'] = in_inlink['sid_target']
            if not in_inlink.has_key("id"):
                in_inlink["id"] = in_inlink["source"] + in_inlink["target"]
        return bundle


class GraphResource(CustomReversionResource):
    """
    """
    concepts = fields.ManyToManyField(ConceptResource, 'concepts', full=True)
    def alter_deserialized_detail_data(self, request, data):
        # create the graph if it does not exist and associate the user with the graph
        for concept in data["concepts"]:
            normalize_concept(concept)
        return data

    def post_save_hook(self, bundle):
        # FIXME we're assuming a user is logged in
        gsettings, gsnew = GraphSettings.objects.get_or_create(graph=bundle.obj)
        uprof, created = Profile.objects.get_or_create(pk=bundle.request.user.pk)
        # TODO add check that the edit actally made a difference
        gsettings.editors.add(uprof)
        gsettings.save()
        return bundle

    class Meta:
        """ GraphResource Meta """
        allowed_methods = ("get", "post", "put", "delete", "patch")
        max_limit = 0
        include_resource_uri = False
        queryset = Graph.objects.all()
        resource_name = 'graph'
        authorization = ModAndUserObjectsOnlyAuthorization()

# helper methods
CONCEPT_SAVE_FIELDS = ["id", "tag", "title", "summary", "goals", "exercises", "software", "pointers", "is_shortcut", "flags", "dependencies", "resources"]
def normalize_concept(in_concept):
    """
    Temporary hack to normalize tag/id for new and old data and remove client-side fields
    """
    if type(in_concept) != dict:
        return

    if not in_concept["id"] or in_concept["id"][:4] == "-new":
        useid = ''
        while not useid or not len(Concept.objects.filter(id=useid)) == 0:
            useid = ''.join([random.choice(string.lowercase + string.digits) for i in range(8)])
        usetag = useid
    elif in_concept.has_key("sid") and len(in_concept["sid"]):
        useid = in_concept["sid"]
        usetag = in_concept["id"]
    else:
        useid = in_concept["id"]
        usetag = in_concept["id"]
    in_concept["id"] = useid
    in_concept["tag"] = usetag

    for field in in_concept.keys():
        if field not in CONCEPT_SAVE_FIELDS:
            del in_concept[field]
