import re
import logging
import functools
import traceback
import prison
import jsonschema
from apispec.exceptions import DuplicateComponentNameError
from sqlalchemy.exc import IntegrityError
from marshmallow import ValidationError
from marshmallow_sqlalchemy.fields import Related, RelatedList
from flask import Blueprint, make_response, jsonify, request, current_app
from werkzeug.exceptions import BadRequest
from flask_babel import lazy_gettext as _
from .convert import Model2SchemaConverter
from .schemas import get_list_schema, get_item_schema, get_info_schema
from ..security.decorators import permission_name, protect
from .._compat import as_unicode
from ..const import (
    API_URI_RIS_KEY,
    API_ORDER_COLUMNS_RES_KEY,
    API_LABEL_COLUMNS_RES_KEY,
    API_LIST_COLUMNS_RES_KEY,
    API_DESCRIPTION_COLUMNS_RES_KEY,
    API_SHOW_COLUMNS_RES_KEY,
    API_ADD_COLUMNS_RES_KEY,
    API_EDIT_COLUMNS_RES_KEY,
    API_FILTERS_RES_KEY,
    API_PERMISSIONS_RES_KEY,
    API_RESULT_RES_KEY,
    API_ORDER_COLUMNS_RIS_KEY,
    API_LABEL_COLUMNS_RIS_KEY,
    API_LIST_COLUMNS_RIS_KEY,
    API_DESCRIPTION_COLUMNS_RIS_KEY,
    API_SHOW_COLUMNS_RIS_KEY,
    API_ADD_COLUMNS_RIS_KEY,
    API_EDIT_COLUMNS_RIS_KEY,
    API_SELECT_COLUMNS_RIS_KEY,
    API_FILTERS_RIS_KEY,
    API_PERMISSIONS_RIS_KEY,
    API_ORDER_COLUMN_RIS_KEY,
    API_ORDER_DIRECTION_RIS_KEY,
    API_PAGE_INDEX_RIS_KEY,
    API_PAGE_SIZE_RIS_KEY,
    API_LIST_TITLE_RES_KEY,
    API_ADD_TITLE_RES_KEY,
    API_EDIT_TITLE_RES_KEY,
    API_SHOW_TITLE_RES_KEY,
    API_LIST_TITLE_RIS_KEY,
    API_ADD_TITLE_RIS_KEY,
    API_EDIT_TITLE_RIS_KEY,
    API_SHOW_TITLE_RIS_KEY
)

log = logging.getLogger(__name__)


def get_error_msg():
    """
        (inspired on Superset code)
    :return:
    """
    if current_app.config.get("FAB_API_SHOW_STACKTRACE"):
        return traceback.format_exc()
    return "Fatal error"


def safe(f):
    """
    A decorator that catches uncaught exceptions and
    return the response in JSON format (inspired on Superset code)
    """

    def wraps(self, *args, **kwargs):
        try:
            return f(self, *args, **kwargs)
        except BadRequest as e:
            return self.response_400(message=str(e))
        except Exception as e:
            logging.exception(e)
            return self.response_500(message=get_error_msg())

    return functools.update_wrapper(wraps, f)


def rison(schema=None):
    """
        Use this decorator to parse URI *Rison* arguments to
        a python data structure, your method gets the data
        structure on kwargs['rison']. Response is HTTP 400
        if *Rison* is not correct::

            class ExampleApi(BaseApi):
                    @expose('/risonjson')
                    @rison()
                    def rison_json(self, **kwargs):
                        return self.response(200, result=kwargs['rison'])

        You can additionally pass a JSON schema to
        validate Rison arguments::

            schema = {
                "type": "object",
                "properties": {
                    "arg1": {
                        "type": "integer"
                    }
                }
            }

            class ExampleApi(BaseApi):
                    @expose('/risonjson')
                    @rison(schema)
                    def rison_json(self, **kwargs):
                        return self.response(200, result=kwargs['rison'])

    """

    def _rison(f):
        def wraps(self, *args, **kwargs):
            value = request.args.get(API_URI_RIS_KEY, None)
            kwargs['rison'] = dict()
            if value:
                try:
                    kwargs['rison'] = \
                        prison.loads(value)
                except prison.decoder.ParserException:
                    return self.response_400(message="Not a valid rison argument")
            if schema:
                try:
                    jsonschema.validate(instance=kwargs['rison'], schema=schema)
                except jsonschema.ValidationError as e:
                    return self.response_400(
                        message="Not a valid rison schema {}".format(e)
                    )
            return f(self, *args, **kwargs)
        return functools.update_wrapper(wraps, f)
    return _rison


def expose(url='/', methods=('GET',)):
    """
        Use this decorator to expose API endpoints on your API classes.

        :param url:
            Relative URL for the endpoint
        :param methods:
            Allowed HTTP methods. By default only GET is allowed.
    """

    def wrap(f):
        if not hasattr(f, '_urls'):
            f._urls = []
        f._urls.append((url, methods))
        return f

    return wrap


def merge_response_func(func, key):
    """
        Use this decorator to set a new merging
        response function to HTTP endpoints

        candidate function must have the following signature
        and be childs of BaseApi:
        ```
            def merge_some_function(self, response, rison_args):
        ```

    :param func: Name of the merge function where the key is allowed
    :param key: The key name for rison selection
    :return: None
    """

    def wrap(f):
        if not hasattr(f, '_response_key_func_mappings'):
            f._response_key_func_mappings = dict()
        f._response_key_func_mappings[key] = func
        return f

    return wrap


class BaseApi(object):
    """
        All apis inherit from this class.
        it's constructor will register your exposed urls on flask
        as a Blueprint.

        This class does not expose any urls,
        but provides a common base for all APIS.
    """

    appbuilder = None
    blueprint = None
    endpoint = None

    version = 'v1'
    """
        Define the Api version for this resource/class
    """
    route_base = None
    """ 
        Define the route base where all methods will suffix from 
    """
    resource_name = None
    """
        Defines a custom resource name, overrides the inferred from Class name 
        makes no sense to use it with route base
    """
    base_permissions = None
    """
        A list of allowed base permissions::
        
            class ExampleApi(BaseApi):
                base_permissions = ['can_get']
                
    """
    allow_browser_login = False
    """
        Will allow flask-login cookie authorization on the API
        default is False.
    """
    extra_args = None

    apispec_parameter_schemas = None
    """
        Set your custom Rison parameter schemas here so that
        they get registered on the OpenApi spec::
        
            custom_parameter = {
                "type": "object"
                "properties": {
                    "name": {
                        "type": "string"
                    }
                }
            }
        
            class CustomApi(BaseApi):
                apispec_parameter_schemas = {
                    "custom_parameter": custom_parameter
                }
    """
    _apispec_parameter_schemas = None

    responses = {
        "400": {
            "description": "Bad request",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string"
                            }
                        }
                    }
                }
            }
        },
        "401": {
            "description": "Unauthorized",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string"
                            }
                        }
                    }
                }
            }
        },
        "404": {
            "description": "Not found",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string"
                            }
                        }
                    }
                }
            }
        },
        "422": {
            "description": "Could not process entity",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string"
                            }
                        }
                    }
                }
            }
        },
        "500": {
            "description": "Fatal error",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string"
                            }
                        }
                    }
                }
            }
        }
    }
    """
        Override custom OpenApi responses
    """

    def __init__(self):
        """
            Initialization of base permissions
            based on exposed methods and actions

            Initialization of extra args
        """
        self._response_key_func_mappings = dict()
        self.apispec_parameter_schemas = self.apispec_parameter_schemas or dict()
        self._apispec_parameter_schemas = self._apispec_parameter_schemas or dict()
        self._apispec_parameter_schemas.update(self.apispec_parameter_schemas)
        if self.base_permissions is None:
            self.base_permissions = set()
            for attr_name in dir(self):
                if hasattr(getattr(self, attr_name), '_permission_name'):
                    _permission_name = \
                        getattr(getattr(self, attr_name), '_permission_name')
                    self.base_permissions.add('can_' + _permission_name)
            self.base_permissions = list(self.base_permissions)
        if not self.extra_args:
            self.extra_args = dict()
        self._apis = dict()
        for attr_name in dir(self):
            if hasattr(getattr(self, attr_name), '_extra'):
                _extra = getattr(getattr(self, attr_name), '_extra')
                for key in _extra: self._apis[key] = _extra[key]

    def create_blueprint(self, appbuilder,
                         endpoint=None,
                         static_folder=None):
        # Store appbuilder instance
        self.appbuilder = appbuilder
        # If endpoint name is not provided, get it from the class name
        self.endpoint = endpoint or self.__class__.__name__
        self.resource_name = self.resource_name or self.__class__.__name__

        if self.route_base is None:
            self.route_base = \
                "/api/{}/{}".format(self.version,
                                    self.resource_name.lower())
        self.blueprint = Blueprint(self.endpoint, __name__,
                                   url_prefix=self.route_base)

        self._register_urls()
        self.add_apispec_components()
        return self.blueprint

    def add_apispec_components(self):
        for k, v in self.responses.items():
            self.appbuilder.apispec.components._responses[k] = v
        for k, v in self._apispec_parameter_schemas.items():
            if k not in self.appbuilder.apispec.components._parameters:
                _v = {
                    "in": "query",
                    "name": API_URI_RIS_KEY,
                    "schema": {"$ref": "#/components/schemas/{}".format(k)}
                }
                # Using private because parameter method does not behave correctly
                self.appbuilder.apispec.components._schemas[k] = v
                self.appbuilder.apispec.components._parameters[k] = _v

    def _register_urls(self):
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if hasattr(attr, '_urls'):
                for url, methods in attr._urls:
                    self.blueprint.add_url_rule(
                        url,
                        attr_name,
                        attr,
                        methods=methods
                    )
                    operations = dict()
                    path = self.path_helper(path=url, operations=operations)
                    self.operation_helper(
                        path=path,
                        operations=operations,
                        methods=methods,
                        func=attr
                    )
                    self.appbuilder.apispec.path(
                        path=path,
                        operations=operations
                    )
                    for operation in operations:
                        self.appbuilder.apispec._paths[path][operation]['tags'] = [
                            self.__class__.__name__
                        ]

    def path_helper(self, path=None, operations=None, **kwargs):
        """
            Works like a apispec plugin
            May return a path as string and mutate operations dict.

        :param str path: Path to the resource
        :param dict operations: A `dict` mapping HTTP methods to operation object. See
            https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#operationObject
        :param kwargs:
        :return: Return value should be a string or None. If a string is returned, it
        is set as the path.
        """
        RE_URL = re.compile(r'<(?:[^:<>]+:)?([^<>]+)>')
        path = RE_URL.sub(r'{\1}', path)
        return "{}{}".format(self.blueprint.url_prefix, path)

    def operation_helper(
            self, path=None,
            operations=None,
            methods=None,
            func=None,
            **kwargs):
        """May mutate operations.
        :param str path: Path to the resource
        :param dict operations: A `dict` mapping HTTP methods to operation object. See
        :param list methods: A list of methods registered for this path
        """
        import yaml
        from apispec import yaml_utils

        for method in methods:
            yaml_doc_string = yaml_utils.load_operations_from_docstring(func.__doc__)
            yaml_doc_string = yaml.safe_load(str(yaml_doc_string).replace(
                "{{self.__class__.__name__}}",
                self.__class__.__name__))
            if yaml_doc_string:
                operations[method.lower()] = yaml_doc_string.get(method.lower(), {})
            else:
                operations[method.lower()] = {}

    @staticmethod
    def _prettify_name(name):
        """
            Prettify pythonic variable name.

            For example, 'HelloWorld' will be converted to 'Hello World'

            :param name:
                Name to prettify.
        """
        return re.sub(r'(?<=.)([A-Z])', r' \1', name)

    @staticmethod
    def _prettify_column(name):
        """
            Prettify pythonic variable name.

            For example, 'hello_world' will be converted to 'Hello World'

            :param name:
                Name to prettify.
        """
        return re.sub('[._]', ' ', name).title()

    def get_uninit_inner_views(self):
        """
            Will return a list with views that need to be initialized.
            Normally related_views from ModelView
        """
        return []

    def get_init_inner_views(self, views):
        """
            Sets initialized inner views
        """
        pass

    def set_response_key_mappings(self, response, func, rison_args, **kwargs):
        if not hasattr(func, '_response_key_func_mappings'):
            return
        _keys = rison_args.get('keys', None)
        if not _keys:
            for k, v in func._response_key_func_mappings.items():
                v(self, response, **kwargs)
        else:
            for k, v in func._response_key_func_mappings.items():
                if k in _keys:
                    v(self, response, **kwargs)

    def merge_current_user_permissions(self, response, **kwargs):
        response[API_PERMISSIONS_RES_KEY] = \
            self.appbuilder.sm.get_user_permissions_on_view(
                self.__class__.__name__
            )

    @staticmethod
    def response(code, **kwargs):
        """
            Generic HTTP JSON response method

        :param code: HTTP code (int)
        :param kwargs: Data structure for response (dict)
        :return: HTTP Json response
        """
        _ret_json = jsonify(kwargs)
        resp = make_response(_ret_json, code)
        resp.headers['Content-Type'] = "application/json; charset=utf-8"
        return resp

    def response_400(self, message=None):
        """
            Helper method for HTTP 400 response

        :param message: Error message (str)
        :return: HTTP Json response
        """
        message = message or "Arguments are not correct"
        return self.response(400, **{"message": message})

    def response_422(self, message=None):
        """
            Helper method for HTTP 422 response

        :param message: Error message (str)
        :return: HTTP Json response
        """
        message = message or "Could not process entity"
        return self.response(422, **{"message": message})

    def response_401(self):
        """
            Helper method for HTTP 401 response

        :param message: Error message (str)
        :return: HTTP Json response
        """
        return self.response(401, **{"message": "Not authorized"})

    def response_404(self):
        """
            Helper method for HTTP 404 response

        :param message: Error message (str)
        :return: HTTP Json response
        """
        return self.response(404, **{"message": "Not found"})

    def response_500(self, message=None):
        """
            Helper method for HTTP 500 response

        :param message: Error message (str)
        :return: HTTP Json response
        """
        message = message or "Internal error"
        return self.response(500, **{"message": message})


class BaseModelApi(BaseApi):
    datamodel = None
    """
        Your sqla model you must initialize it like::

            class MyModelApi(BaseModelApi):
                datamodel = SQLAInterface(MyTable)
    """
    search_columns = None
    """
        List with allowed search columns, if not provided all possible search 
        columns will be used. If you want to limit the search (*filter*) columns
         possibilities, define it with a list of column names from your model::

            class MyView(ModelRestApi):
                datamodel = SQLAInterface(MyTable)
                search_columns = ['name', 'address']

    """
    search_exclude_columns = None
    """
        List with columns to exclude from search. Search includes all possible 
        columns by default
    """
    label_columns = None
    """
        Dictionary of labels for your columns, override this if you want
         different pretify labels

        example (will just override the label for name column)::

            class MyView(ModelRestApi):
                datamodel = SQLAInterface(MyTable)
                label_columns = {'name':'My Name Label Override'}

    """
    base_filters = None
    """
        Filter the view use: [['column_name',BaseFilter,'value'],]

        example::

            def get_user():
                return g.user

            class MyView(ModelRestApi):
                datamodel = SQLAInterface(MyTable)
                base_filters = [['created_by', FilterEqualFunction, get_user],
                                ['name', FilterStartsWith, 'a']]

    """

    base_order = None
    """
        Use this property to set default ordering for lists
         ('col_name','asc|desc')::

            class MyView(ModelRestApi):
                datamodel = SQLAInterface(MyTable)
                base_order = ('my_column_name','asc')

    """
    _base_filters = None
    """ Internal base Filter from class Filters will always filter view """
    _filters = None
    """ 
        Filters object will calculate all possible filter types 
        based on search_columns 
    """

    def __init__(self, **kwargs):
        """
            Constructor
        """
        datamodel = kwargs.get('datamodel', None)
        if datamodel:
            self.datamodel = datamodel
        self._init_properties()
        self._init_titles()
        super(BaseModelApi, self).__init__()

    def _gen_labels_columns(self, list_columns):
        """
            Auto generates pretty label_columns from list of columns
        """
        for col in list_columns:
            if not self.label_columns.get(col):
                self.label_columns[col] = self._prettify_column(col)

    def _label_columns_json(self, cols=None):
        """
            Prepares dict with labels to be JSON serializable
        """
        ret = {}
        cols = cols or []
        d = {k: v for (k, v) in self.label_columns.items() if k in cols}
        for key, value in d.items():
            ret[key] = as_unicode(_(value).encode('UTF-8'))
        return ret

    def _init_properties(self):
        self.label_columns = self.label_columns or {}
        self.base_filters = self.base_filters or []
        self.search_exclude_columns = self.search_exclude_columns or []
        self.search_columns = self.search_columns or []

        self._base_filters = \
            self.datamodel.get_filters().add_filter_list(self.base_filters)
        list_cols = self.datamodel.get_columns_list()
        search_columns = self.datamodel.get_search_columns_list()
        if not self.search_columns:
            self.search_columns = \
                [x for x in search_columns if x not in self.search_exclude_columns]

        self._gen_labels_columns(list_cols)
        self._filters = self.datamodel.get_filters(self.search_columns)

    def _init_titles(self):
        pass


class ModelRestApi(BaseModelApi):
    list_title = ""
    """ 
        List Title, if not configured the default is 
        'List ' with pretty model name 
    """
    show_title = ""
    """
        Show Title , if not configured the default is 
        'Show ' with pretty model name 
    """
    add_title = ""
    """ 
        Add Title , if not configured the default is 
        'Add ' with pretty model name
    """
    edit_title = ""
    """ 
        Edit Title , if not configured the default is 
        'Edit ' with pretty model name
    """

    list_columns = None
    """
        A list of columns (or model's methods) to be displayed on the list view.
        Use it to control the order of the display
    """
    show_columns = None
    """
        A list of columns (or model's methods) for the get item endpoint.
        Use it to control the order of the results
    """
    add_columns = None
    """
        A list of columns (or model's methods) to be allowed to post 
    """
    edit_columns = None
    """
        A list of columns (or model's methods) to be allowed to update
    """
    list_exclude_columns = None
    """
        A list of columns to exclude from the get list endpoint. 
        By default all columns are included.
    """
    show_exclude_columns = None
    """
        A list of columns to exclude from the get item endpoint. 
        By default all columns are included.
    """
    add_exclude_columns = None
    """
        A list of columns to exclude from the add endpoint.
        By default all columns are included.
    """
    edit_exclude_columns = None
    """
        A list of columns to exclude from the edit endpoint.
        By default all columns are included.
    """
    order_columns = None
    """ Allowed order columns """
    page_size = 20
    """
        Use this property to change default page size
    """
    description_columns = None
    """
        Dictionary with column descriptions that will be shown on the forms::

            class MyView(ModelView):
                datamodel = SQLAModel(MyTable, db.session)

                description_columns = {'name':'your models name column',
                                        'address':'the address column'}
    """
    validators_columns = None
    """ Dictionary to add your own validators for forms """

    add_query_rel_fields = None
    """
        Add Customized query for related add fields.
        Assign a dictionary where the keys are the column names of
        the related models to filter, the value for each key, is a list of lists with the
        same format as base_filter
        {'relation col name':[['Related model col',FilterClass,'Filter Value'],...],...}
        Add a custom filter to form related fields::
    
            class ContactModelView(ModelRestApi):
                datamodel = SQLAModel(Contact)
                add_query_rel_fields = {'group':[['name',FilterStartsWith,'W']]}
    
    """
    edit_query_rel_fields = None
    """
        Add Customized query for related edit fields.
        Assign a dictionary where the keys are the column names of
        the related models to filter, the value for each key, is a list of lists with the
        same format as base_filter
        {'relation col name':[['Related model col',FilterClass,'Filter Value'],...],...}
        Add a custom filter to form related fields::
    
            class ContactModelView(ModelRestApi):
                datamodel = SQLAModel(Contact, db.session)
                edit_query_rel_fields = {'group':[['name',FilterStartsWith,'W']]}
    
    """
    order_rel_fields = None
    """
        Impose order on related fields.
        assign a dictionary where the keys are the related column names::
        
            class ContactModelView(ModelRestApi):
                datamodel = SQLAModel(Contact)
                order_rel_fields = {
                    'group': ('name', 'asc')
                    'gender': ('name', 'asc')
                }
    """
    list_model_schema = None
    """
        Override to provide your own marshmallow Schema 
        for JSON to SQLA dumps
    """
    add_model_schema = None
    """
        Override to provide your own marshmallow Schema 
        for JSON to SQLA dumps
    """
    edit_model_schema = None
    """
        Override to provide your own marshmallow Schema 
        for JSON to SQLA dumps
    """
    show_model_schema = None
    """
        Override to provide your own marshmallow Schema 
        for JSON to SQLA dumps
    """
    model2schemaconverter = Model2SchemaConverter
    """
        Override to use your own Model2SchemaConverter 
        (inherit from BaseModel2SchemaConverter)
    """
    _apispec_parameter_schemas = {
        "get_info_schema": get_info_schema,
        "get_item_schema": get_item_schema,
        "get_list_schema": get_list_schema
    }

    def __init__(self):
        super(ModelRestApi, self).__init__()
        self.validators_columns = self.validators_columns or {}
        self.model2schemaconverter = self.model2schemaconverter(
            self.datamodel,
            self.validators_columns
        )

    def create_blueprint(self, appbuilder, *args, **kwargs):
        self._init_model_schemas()
        return super(ModelRestApi, self).create_blueprint(
            appbuilder,
            *args,
            **kwargs
        )

    def add_apispec_components(self):
        super(ModelRestApi, self).add_apispec_components()
        self.appbuilder.apispec.components.schema(
            "{}.{}".format(self.__class__.__name__, "get_list"),
            schema=self.list_model_schema
        )
        self.appbuilder.apispec.components.schema(
            "{}.{}".format(self.__class__.__name__, "post"),
            schema=self.add_model_schema
        )
        self.appbuilder.apispec.components.schema(
            "{}.{}".format(self.__class__.__name__, "put"),
            schema=self.edit_model_schema
        )
        self.appbuilder.apispec.components.schema(
            "{}.{}".format(self.__class__.__name__, "get"),
            schema=self.show_model_schema
        )

    def _init_model_schemas(self):
        # Create Marshmalow schemas if one is not specified
        if self.list_model_schema is None:
            self.list_model_schema = \
                self.model2schemaconverter.convert(self.list_columns)
        if self.add_model_schema is None:
            self.add_model_schema = \
                self.model2schemaconverter.convert(
                    self.add_columns,
                    nested=False,
                    enum_dump_by_name=True
                )
        if self.edit_model_schema is None:
            self.edit_model_schema = \
                self.model2schemaconverter.convert(
                    self.edit_columns,
                    nested=False,
                    enum_dump_by_name=True
                )
        if self.show_model_schema is None:
            self.show_model_schema = \
                self.model2schemaconverter.convert(self.show_columns)

    def _init_titles(self):
        """
            Init Titles if not defined
        """
        super(ModelRestApi, self)._init_titles()
        class_name = self.datamodel.model_name
        if not self.list_title:
            self.list_title = 'List ' + self._prettify_name(class_name)
        if not self.add_title:
            self.add_title = 'Add ' + self._prettify_name(class_name)
        if not self.edit_title:
            self.edit_title = 'Edit ' + self._prettify_name(class_name)
        if not self.show_title:
            self.show_title = 'Show ' + self._prettify_name(class_name)
        self.title = self.list_title

    def _init_properties(self):
        """
            Init Properties
        """
        super(ModelRestApi, self)._init_properties()
        # Reset init props
        self.description_columns = self.description_columns or {}
        self.list_exclude_columns = self.list_exclude_columns or []
        self.show_exclude_columns = self.show_exclude_columns or []
        self.add_exclude_columns = self.add_exclude_columns or []
        self.edit_exclude_columns = self.edit_exclude_columns or []
        self.order_rel_fields = self.order_rel_fields or {}
        # Generate base props
        list_cols = self.datamodel.get_user_columns_list()
        if not self.list_columns and self.list_model_schema:
            list(self.list_model_schema._declared_fields.keys())
        else:
            self.list_columns = self.list_columns or [
                x for x in self.datamodel.get_user_columns_list()
                if x not in self.list_exclude_columns
            ]

        self._gen_labels_columns(self.list_columns)
        self.order_columns = self.order_columns or \
                             self.datamodel.get_order_columns_list(
                                 list_columns=self.list_columns
                             )
        # Process excluded columns
        if not self.show_columns:
            self.show_columns = \
                [x for x in list_cols if x not in self.show_exclude_columns]
        if not self.add_columns:
            self.add_columns = \
                [x for x in list_cols if x not in self.add_exclude_columns]
        if not self.edit_columns:
            self.edit_columns = \
                [x for x in list_cols if x not in self.edit_exclude_columns]
        self._filters = self.datamodel.get_filters(self.search_columns)
        self.edit_query_rel_fields = self.edit_query_rel_fields or dict()
        self.add_query_rel_fields = self.add_query_rel_fields or dict()

    def merge_add_field_info(self, response, **kwargs):
        _kwargs = kwargs.get('add_columns', {})
        response[API_ADD_COLUMNS_RES_KEY] = \
            self._get_fields_info(
                self.add_columns,
                self.add_model_schema,
                self.add_query_rel_fields,
                **_kwargs
            )

    def merge_edit_field_info(self, response, **kwargs):
        _kwargs = kwargs.get('edit_columns', {})
        response[API_EDIT_COLUMNS_RES_KEY] = \
            self._get_fields_info(
                self.edit_columns,
                self.edit_model_schema,
                self.edit_query_rel_fields,
                **_kwargs
            )

    def merge_search_filters(self, response, **kwargs):
        # Get possible search fields and all possible operations
        search_filters = dict()
        dict_filters = self._filters.get_search_filters()
        for col in self.search_columns:
            search_filters[col] = [
                {'name': as_unicode(flt.name),
                 'operator': flt.arg_name} for flt in dict_filters[col]
            ]
        response[API_FILTERS_RES_KEY] = search_filters

    def merge_add_title(self, response, **kwargs):
        response[API_ADD_TITLE_RES_KEY] = self.add_title

    def merge_edit_title(self, response, **kwargs):
        response[API_EDIT_TITLE_RES_KEY] = self.edit_title

    def merge_label_columns(self, response, **kwargs):
        _pruned_select_cols = kwargs.get(API_SELECT_COLUMNS_RIS_KEY, [])
        if _pruned_select_cols:
            _show_columns = _pruned_select_cols
        else:
            _show_columns = self.show_columns
        response[API_LABEL_COLUMNS_RES_KEY] = self._label_columns_json(_show_columns)

    def merge_show_columns(self, response, **kwargs):
        _pruned_select_cols = kwargs.get(API_SELECT_COLUMNS_RIS_KEY, [])
        if _pruned_select_cols:
            response[API_SHOW_COLUMNS_RES_KEY] = _pruned_select_cols
        else:
            response[API_SHOW_COLUMNS_RES_KEY] = self.show_columns

    def merge_description_columns(self, response, **kwargs):
        _pruned_select_cols = kwargs.get(API_SELECT_COLUMNS_RIS_KEY, [])
        if _pruned_select_cols:
            response[API_DESCRIPTION_COLUMNS_RES_KEY] = \
                self._description_columns_json(_pruned_select_cols)
        else:
            response[API_DESCRIPTION_COLUMNS_RES_KEY] = \
                self._description_columns_json(self.show_columns)

    def merge_list_columns(self, response, **kwargs):
        _pruned_select_cols = kwargs.get(API_SELECT_COLUMNS_RIS_KEY, [])
        if _pruned_select_cols:
            response[API_LIST_COLUMNS_RES_KEY] = _pruned_select_cols
        else:
            response[API_LIST_COLUMNS_RES_KEY] = self.list_columns

    def merge_order_columns(self, response, **kwargs):
        _pruned_select_cols = kwargs.get(API_SELECT_COLUMNS_RIS_KEY, [])
        if _pruned_select_cols:
            response[API_ORDER_COLUMNS_RES_KEY] = [
                order_col
                for order_col in self.order_columns if order_col in _pruned_select_cols
            ]
        else:
            response[API_ORDER_COLUMNS_RES_KEY] = self.order_columns

    def merge_list_title(self, response, **kwargs):
        response[API_LIST_TITLE_RES_KEY] = self.list_title

    def merge_show_title(self, response, **kwargs):
        response[API_SHOW_TITLE_RES_KEY] = self.show_title

    @expose('/_info', methods=['GET'])
    @protect()
    @safe
    @rison(get_info_schema)
    @permission_name('info')
    @merge_response_func(BaseApi.merge_current_user_permissions, API_PERMISSIONS_RIS_KEY)
    @merge_response_func(merge_add_field_info, API_ADD_COLUMNS_RIS_KEY)
    @merge_response_func(merge_edit_field_info, API_EDIT_COLUMNS_RIS_KEY)
    @merge_response_func(merge_search_filters, API_FILTERS_RIS_KEY)
    @merge_response_func(merge_add_title, API_ADD_TITLE_RIS_KEY)
    @merge_response_func(merge_edit_title, API_EDIT_TITLE_RIS_KEY)
    def info(self, **kwargs):
        """ Endpoint that renders a response for CRUD REST meta data
        ---
        get:
          parameters:
          - $ref: '#/components/parameters/get_info_schema'
          responses:
            200:
              description: Item from Model
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      add_columns:
                        type: object
                      edit_columns:
                        type: object
                      filters:
                        type: object
                      permissions:
                        type: array
                        items:
                          type: string
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            422:
              $ref: '#/components/responses/422'
            500:
              $ref: '#/components/responses/500'
        """
        _response = dict()
        _args = kwargs.get('rison', {})
        self.set_response_key_mappings(_response, self.info, _args, **_args)
        return self.response(200, **_response)

    @expose('/<pk>', methods=['GET'])
    @protect()
    @safe
    @permission_name('get')
    @rison(get_item_schema)
    @merge_response_func(merge_label_columns, API_LABEL_COLUMNS_RIS_KEY)
    @merge_response_func(merge_show_columns, API_SHOW_COLUMNS_RIS_KEY)
    @merge_response_func(merge_description_columns, API_DESCRIPTION_COLUMNS_RIS_KEY)
    @merge_response_func(merge_show_title, API_SHOW_TITLE_RIS_KEY)
    def get(self, pk, **kwargs):
        """Get item from Model
        ---
        get:
          parameters:
          - in: path
            schema:
              type: integer
            name: pk
          - $ref: '#/components/parameters/get_item_schema'
          responses:
            200:
              description: Item from Model
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      label_columns:
                        type: object
                      show_columns:
                        type: array
                        items:
                          type: string
                      description_columns:
                        type: object
                      show_title:
                        type: string
                      id:
                        type: string
                      result:
                        $ref: '#/components/schemas/{{self.__class__.__name__}}.get'
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
            500:
              $ref: '#/components/responses/500'
        """
        item = self.datamodel.get(pk, self._base_filters)
        if not item:
            return self.response_404()

        _response = dict()
        _args = kwargs.get('rison', {})
        select_cols = _args.get(API_SELECT_COLUMNS_RIS_KEY, [])
        _pruned_select_cols = [
            col for col in select_cols if col in self.show_columns
        ]
        self.set_response_key_mappings(
            _response,
            self.get,
            _args,
            **{API_SELECT_COLUMNS_RIS_KEY: _pruned_select_cols}
        )
        if _pruned_select_cols:
            _show_model_schema = self.model2schemaconverter.convert(_pruned_select_cols)
        else:
            _show_model_schema = self.show_model_schema

        _response['id'] = pk
        _response[API_RESULT_RES_KEY] = _show_model_schema.dump(item, many=False).data
        return self.response(200, **_response)

    @expose('/', methods=['GET'])
    @protect()
    @safe
    @permission_name('get')
    @rison(get_list_schema)
    @merge_response_func(merge_order_columns, API_ORDER_COLUMNS_RIS_KEY)
    @merge_response_func(merge_label_columns, API_LABEL_COLUMNS_RIS_KEY)
    @merge_response_func(merge_description_columns, API_DESCRIPTION_COLUMNS_RIS_KEY)
    @merge_response_func(merge_list_columns, API_LIST_COLUMNS_RIS_KEY)
    @merge_response_func(merge_list_title, API_LIST_TITLE_RIS_KEY)
    def get_list(self, **kwargs):
        """Get list of items from Model
        ---
        get:
          parameters:
          - $ref: '#/components/parameters/get_item_schema'
          responses:
            200:
              description: Items from Model
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      label_columns:
                        type: object
                      list_columns:
                        type: array
                        items:
                          type: string
                      description_columns:
                        type: object
                      list_title:
                        type: string
                      ids:
                        type: array
                        items:
                          type: string
                      order_columns:
                        type: array
                        items:
                          type: string
                      result:
                        $ref: '#/components/schemas/{{self.__class__.__name__}}.get_list'
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            422:
              $ref: '#/components/responses/422'
            500:
              $ref: '#/components/responses/500'
        """
        _response = dict()
        _args = kwargs.get('rison', {})
        # handle select columns
        select_cols = _args.get(API_SELECT_COLUMNS_RIS_KEY, [])
        _pruned_select_cols = [col for col in select_cols if col in self.list_columns]
        self.set_response_key_mappings(
            _response,
            self.get_list,
            _args,
            **{API_SELECT_COLUMNS_RIS_KEY: _pruned_select_cols}
        )

        if _pruned_select_cols:
            _list_model_schema = self.model2schemaconverter.convert(_pruned_select_cols)
        else:
            _list_model_schema = self.list_model_schema
        # handle filters
        joined_filters = self._handle_filters_args(_args)
        # handle base order
        order_column, order_direction = self._handle_order_args(_args)
        # handle pagination
        page_index, page_size = self._handle_page_args(_args)
        # Make the query
        query_select_columns = _pruned_select_cols or self.list_columns
        count, lst = self.datamodel.query(
            joined_filters,
            order_column,
            order_direction,
            page=page_index,
            page_size=page_size,
            select_columns=query_select_columns
        )
        pks = self.datamodel.get_keys(lst)
        _response[API_RESULT_RES_KEY] = _list_model_schema.dump(lst, many=True).data
        _response['ids'] = pks
        _response['count'] = count
        return self.response(200, **_response)

    @expose('/', methods=['POST'])
    @protect()
    @safe
    @permission_name('post')
    def post(self):
        """POST item to Model
        ---
        post:
          responses:
            201:
              description: Item inserted
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      id:
                        type: string
                      result:
                        $ref: '#/components/schemas/{{self.__class__.__name__}}.post'
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            422:
              $ref: '#/components/responses/422'
            500:
              $ref: '#/components/responses/500'
        """
        if not request.is_json:
            return self.response_400(message='Request is not JSON')
        try:
            item = self.add_model_schema.load(request.json)
        except ValidationError as err:
            return self.response_422(message=err.messages)
        # This validates custom Schema with custom validations
        if isinstance(item.data, dict):
            return self.response_422(message=item.errors)
        self.pre_add(item.data)
        try:
            self.datamodel.add(item.data, raise_exception=True)
            self.post_add(item.data)
            return self.response(
                201,
                **{
                    API_RESULT_RES_KEY: self.add_model_schema.dump(
                        item.data, many=False
                    ).data,
                    'id': self.datamodel.get_pk_value(item.data)
                }
            )
        except IntegrityError as e:
            return self.response_422(message=str(e.orig))

    @expose('/<pk>', methods=['PUT'])
    @protect()
    @safe
    @permission_name('put')
    def put(self, pk):
        """POST item to Model
        ---
        put:
          parameters:
          - in: path
            schema:
              type: integer
            name: pk
          responses:
            200:
              description: Item changed
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      result:
                        $ref: '#/components/schemas/{{self.__class__.__name__}}.put'
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
            500:
              $ref: '#/components/responses/500'
        """
        item = self.datamodel.get(pk, self._base_filters)
        if not request.is_json:
            return self.response(400, **{'message': 'Request is not JSON'})
        if not item:
            return self.response_404()
        try:
            data = self._merge_update_item(item, request.json)
            item = self.edit_model_schema.load(data, instance=item)
        except ValidationError as err:
            return self.response_422(message=err.messages)
        # This validates custom Schema with custom validations
        if isinstance(item.data, dict):
            return self.response_422(message=item.errors)
        self.pre_update(item.data)
        try:
            self.datamodel.edit(item.data, raise_exception=True)
            self.post_update(item)
            return self.response(
                200,
                **{API_RESULT_RES_KEY: self.edit_model_schema.dump(
                    item.data,
                    many=False).data}
            )
        except IntegrityError as e:
            return self.response_422(message=str(e.orig))

    @expose('/<pk>', methods=['DELETE'])
    @protect()
    @safe
    @permission_name('delete')
    def delete(self, pk):
        """Delete item from Model
        ---
        delete:
          parameters:
          - in: path
            schema:
              type: integer
            name: pk
          responses:
            200:
              description: Item deleted
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      message:
                        type: string
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
            500:
              $ref: '#/components/responses/500'
        """
        item = self.datamodel.get(pk, self._base_filters)
        if not item:
            return self.response_404()
        self.pre_delete(item)
        try:
            self.datamodel.delete(item, raise_exception=True)
            self.post_delete(item)
            return self.response(200, message='OK')
        except IntegrityError as e:
            return self.response_422(message=str(e.orig))

    """
    ------------------------------------------------
                HELPER FUNCTIONS
    ------------------------------------------------
    """

    def _handle_page_args(self, rison_args):
        """
            Helper function to handle rison page
            arguments, sets defaults and impose
            FAB_API_MAX_PAGE_SIZE

        :param args:
        :return: (tuple) page, page_size
        """
        page = rison_args.get(API_PAGE_INDEX_RIS_KEY, 0)
        page_size = rison_args.get(API_PAGE_SIZE_RIS_KEY, self.page_size)
        return self._sanitize_page_args(page, page_size)

    def _sanitize_page_args(self, page, page_size):
        _page = page or 0
        _page_size = page_size or self.page_size
        max_page_size = current_app.config.get('FAB_API_MAX_PAGE_SIZE')
        if _page_size > max_page_size or _page_size < 1:
            _page_size = max_page_size
        return _page, _page_size

    def _handle_order_args(self, rison_args):
        """
            Help function to handle rison order
            arguments

        :param rison_args:
        :return:
        """
        order_column = rison_args.get(API_ORDER_COLUMN_RIS_KEY, '')
        order_direction = rison_args.get(API_ORDER_DIRECTION_RIS_KEY, '')
        if not order_column and self.base_order:
            order_column, order_direction = self.base_order
        if order_column not in self.order_columns:
            return '', ''
        return order_column, order_direction

    def _handle_filters_args(self, rison_args):
        self._filters.clear_filters()
        self._filters.rest_add_filters(rison_args.get(API_FILTERS_RIS_KEY, []))
        return self._filters.get_joined_filters(self._base_filters)

    def _description_columns_json(self, cols=None):
        """
            Prepares dict with col descriptions to be JSON serializable
        """
        ret = {}
        cols = cols or []
        d = {k: v for (k, v) in self.description_columns.items() if k in cols}
        for key, value in d.items():
            ret[key] = as_unicode(_(value).encode('UTF-8'))
        return ret

    def _get_field_info(self, field, filter_rel_field, page=None, page_size=None):
        """
            Return a dict with field details
            ready to serve as a response

        :param field: marshmallow field
        :return: dict with field details
        """
        ret = dict()
        ret['name'] = field.name
        ret['label'] = self.label_columns.get(field.name, '')
        ret['description'] = self.description_columns.get(field.name, '')
        # Handles related fields
        if isinstance(field, Related) or isinstance(field, RelatedList):
            ret['count'], ret['values'] = self._get_list_related_field(
                field,
                filter_rel_field,

                page=page,
                page_size=page_size
            )
        if field.validate and isinstance(field.validate, list):
            ret['validate'] = [str(v) for v in field.validate]
        elif field.validate:
            ret['validate'] = [str(field.validate)]
        ret['type'] = field.__class__.__name__
        ret['required'] = field.required
        ret['unique'] = field.unique
        return ret

    def _get_fields_info(self, cols, model_schema, filter_rel_fields, **kwargs):
        """
            Returns a dict with fields detail
            from a marshmallow schema

        :param cols: list of columns to show info for
        :param model_schema: Marshmallow model schema
        :param filter_rel_fields: expects add_query_rel_fields or
                                    edit_query_rel_fields
        :param kwargs: Receives all rison arguments for pagination
        :return: dict with all fields details
        """
        ret = list()
        for col in cols:
            page = page_size = None
            col_args = kwargs.get(col, {})
            if col_args:
                page = col_args.get(API_PAGE_INDEX_RIS_KEY, None)
                page_size = col_args.get(API_PAGE_SIZE_RIS_KEY, None)
            ret.append(self._get_field_info(
                model_schema.fields[col],
                filter_rel_fields.get(col, []),
                page=page,
                page_size=page_size
            ))
        return ret

    def _get_list_related_field(self, field, filter_rel_field, page=None, page_size=None):
        """
            Return a list of values for a related field

        :param field: Marshmallow field
        :param filter_rel_field: Filters for the related field
        :param page: The page index
        :param page_size: The page size
        :return: (int, list) total record count and list of dict with id and value
        """
        ret = list()
        if isinstance(field, Related) or isinstance(field, RelatedList):
            datamodel = self.datamodel.get_related_interface(field.name)
            filters = datamodel.get_filters(
                datamodel.get_search_columns_list()
            )
            page, page_size = self._sanitize_page_args(page, page_size)
            order_field = self.order_rel_fields.get(field.name)
            if order_field:
                order_column, order_direction = order_field
            else:
                order_column, order_direction = '', ''
            if filter_rel_field:
                filters = filters.add_filter_list(filter_rel_field)
                count, values = datamodel.query(
                    filters,
                    order_column,
                    order_direction,
                    page=page,
                    page_size=page_size,
                )
            else:
                count, values = datamodel.query(
                    filters,
                    order_column,
                    order_direction,
                    page=page,
                    page_size=page_size,
                )
            for value in values:
                ret.append(
                    {
                        "id": datamodel.get_pk_value(value),
                        "value": str(value)
                    }
                )
        return count, ret

    def _merge_update_item(self, model_item, data):
        """
            Merge a model with a python data structure
            This is useful to turn PUT method into a PATCH also
        :param model_item: SQLA Model
        :param data: python data structure
        :return: python data structure
        """
        data_item = self.edit_model_schema.dump(model_item, many=False).data
        for _col in self.edit_columns:
            if _col not in data.keys():
                data[_col] = data_item[_col]
        return data

    """
    ------------------------------------------------
                PRE AND POST METHODS
    ------------------------------------------------
    """

    def pre_update(self, item):
        """
            Override this, this method is called before the update takes place.
            If an exception is raised by this method,
            the message is shown to the user and the update operation is
            aborted. Because of this behavior, it can be used as a way to
            implement more complex logic around updates. For instance
            allowing only the original creator of the object to update it.
        """
        pass

    def post_update(self, item):
        """
            Override this, will be called after update
        """
        pass

    def pre_add(self, item):
        """
            Override this, will be called before add.
            If an exception is raised by this method,
            the message is shown to the user and the add operation is aborted.
        """
        pass

    def post_add(self, item):
        """
            Override this, will be called after update
        """
        pass

    def pre_delete(self, item):
        """
            Override this, will be called before delete
            If an exception is raised by this method,
            the message is shown to the user and the delete operation is
            aborted. Because of this behavior, it can be used as a way to
            implement more complex logic around deletes. For instance
            allowing only the original creator of the object to delete it.
        """
        pass

    def post_delete(self, item):
        """
            Override this, will be called after delete
        """
        pass