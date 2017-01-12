# Licenced under the txaws licence available at /LICENSE in the txaws source.

import os
import urlparse
from urllib import quote
from datetime import datetime
from io import BytesIO

try:
    from xml.etree.ElementTree import ParseError
except ImportError:
    from xml.parsers.expat import ExpatError as ParseError

import warnings
from StringIO import StringIO

import attr
from attr import validators

from pyrsistent import PMap, freeze, pmap

from twisted.python.reflect import namedAny
from twisted.logger import Logger
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.ssl import ClientContextFactory
from twisted.internet.protocol import Protocol
from twisted.internet.defer import Deferred, succeed, fail
from twisted.python import failure
from twisted.web import http
from twisted.web.iweb import UNKNOWN_LENGTH, IBodyProducer
from twisted.web.client import Agent, ProxyAgent
from twisted.web.client import ResponseDone
from twisted.web.client import FileBodyProducer
from twisted.web.http import NO_CONTENT, PotentialDataLoss
from twisted.web.http_headers import Headers
from twisted.web.error import Error as TwistedWebError

from txaws.util import parse
from txaws.credentials import AWSCredentials
from txaws.exception import AWSResponseParseError
from txaws.service import AWSServiceEndpoint
from txaws.client.ssl import VerifyingContextFactory
from txaws.client._validators import list_of as _list_of
from txaws import _auth_v4

_log = Logger()

def error_wrapper(error, errorClass):
    """
    We want to see all error messages from cloud services. Amazon's EC2 says
    that their errors are accompanied either by a 400-series or 500-series HTTP
    response code. As such, the first thing we want to do is check to see if
    the error is in that range. If it is, we then need to see if the error
    message is an EC2 one.

    In the event that an error is not a Twisted web error nor an EC2 one, the
    original exception is raised.
    """
    http_status = 0
    if error.check(TwistedWebError):
        xml_payload = error.value.response
        if error.value.status:
            http_status = int(error.value.status)
    else:
        error.raiseException()
    if http_status >= 400:
        if not xml_payload:
            error.raiseException()
        try:
            fallback_error = errorClass(
                xml_payload, error.value.status, str(error.value),
                error.value.response)
        except (ParseError, AWSResponseParseError):
            error_message = http.RESPONSES.get(http_status)
            fallback_error = TwistedWebError(
                http_status, error_message, error.value.response)
        raise fallback_error
    elif 200 <= http_status < 300:
        return str(error.value)
    else:
        error.raiseException()


class BaseClient(object):
    """Create an AWS client.

    @param creds: User authentication credentials to use.
    @param endpoint: The service endpoint URI.
    @param query_factory: The class or function that produces a query
        object for making requests to the EC2 service.
    @param parser: A parser object for parsing responses from the EC2 service.
    @param receiver_factory: Factory for receiving responses from EC2 service.
    """
    def __init__(self, creds=None, endpoint=None, query_factory=None,
                 parser=None, receiver_factory=None):
        if creds is None:
            creds = AWSCredentials()
        if endpoint is None:
            endpoint = AWSServiceEndpoint()
        self.creds = creds
        self.endpoint = endpoint
        self.query_factory = query_factory
        self.receiver_factory = receiver_factory
        self.parser = parser

class StreamingError(Exception):
    """
    Raised if more data or less data is received than expected.
    """


class StreamingBodyReceiver(Protocol):
    """
    Streaming HTTP response body receiver.

    TODO: perhaps there should be an interface specifying why
    finished (Deferred) and content_length are necessary and
    how to used them; eg. callback/errback finished on completion.
    """
    finished = None
    content_length = None

    def __init__(self, fd=None, readback=True):
        """
        @param fd: a file descriptor to write to
        @param readback: if True read back data from fd to callback finished
            with, otherwise we call back finish with fd itself
        with
        """
        if fd is None:
            fd = StringIO()
        self._fd = fd
        self._received = 0
        self._readback = readback

    def dataReceived(self, bytes):
        streaming = self.content_length is UNKNOWN_LENGTH
        if not streaming and (self._received > self.content_length):
            self.transport.loseConnection()
            raise StreamingError(
                "Buffer overflow - received more data than "
                "Content-Length dictated: %d" % self.content_length)
        # TODO should be some limit on how much we receive
        self._fd.write(bytes)
        self._received += len(bytes)

    def connectionLost(self, reason):
        reason.trap(ResponseDone, PotentialDataLoss)
        d = self.finished
        self.finished = None
        streaming = self.content_length is UNKNOWN_LENGTH
        if streaming or (self._received == self.content_length):
            if self._readback:
                self._fd.seek(0)
                data = self._fd.read()
                self._fd.close()
                self._fd = None
                d.callback(data)
            else:
                d.callback(self._fd)
        else:
            f = failure.Failure(StreamingError("Connection lost before "
                "receiving all data"))
            d.errback(f)


class WebClientContextFactory(ClientContextFactory):

    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)


class WebVerifyingContextFactory(VerifyingContextFactory):

    def getContext(self, hostname, port):
        return VerifyingContextFactory.getContext(self)


@attr.s(frozen=True)
class _QueryArgument(object):
    """
    Representation of a single URL query argument, eg I{foo=bar}.
    """
    name = attr.ib(validator=validators.instance_of(unicode))
    value = attr.ib(default=None, validator=validators.optional(validators.instance_of(unicode)))

    def url_encode(self):
        def q(t):
            return quote(t.encode("utf-8"), safe=b"")

        if self.value is None:
            return q(self.name)
        return q(self.name) + b"=" + q(self.value)


def _maybe_tuples_to_queryarg(maybe_tuples):
    """
    Convert an iterator of tuples to a list of L{_QueryArgument}
    instances.

    If the iterator is C{None}, just return C{None}.
    """
    if maybe_tuples is None:
        return None
    return list(_QueryArgument(*v) for v in maybe_tuples)


def url_context(**kw):
    """
    Construct a new URL context, usable to determine the URI to which
    a query should be issued.

    :param unicode scheme: The scheme portion of the URL, ie
        ``u"http"`` or ``u"https"``.

    :param unicode host: The host portion of the URL, ie
        ``u"example.com"``.

    :param int port: A non-default port for the URL or ``None`` for
        the scheme default.

    :param list path: The path portion of the URL as a list of unicode
        path segments.

    :param list query: The query arguments of the URL as a list of
        tuples.  Each tuple is length one (a unicode string
        representing a no-value argument) or two (two unicode strings
        representing an argument name and value).
    """
    return _URLContext(**kw)


@attr.s(frozen=True)
class _URLContext(object):
    """
    A description of the URL involved in an AWS request.

    See parameter documentation for ``url_context`` (the public
    constructor) for details about attributes.

    ``url_context`` is the public constructor to hide the type and
    prevent subclassing.
    """
    scheme = attr.ib(validator=validators.instance_of(unicode))
    host = attr.ib(validator=validators.instance_of(unicode))
    port = attr.ib(validator=validators.optional(validators.instance_of(int)))
    path = attr.ib(validator=_list_of(validators.instance_of(unicode)))
    query = attr.ib(
        default=None,
        convert=_maybe_tuples_to_queryarg,
        validator=validators.optional(_list_of(validators.instance_of(_QueryArgument))),
    )

    def get_encoded_host(self):
        """
        :return bytes: The encoded host component.
        """
        return self.host.encode("idna")


    def get_encoded_path(self):
        """
        :return bytes: The encoded path component.
        """
        return b"/" + b"/".join(
            quote(segment.encode("utf-8"), safe=b"") for segment in self.path
        )


    def get_encoded_query(self):
        """
        :return bytes: The encoded query component.
        """

        if self.query is None:
            return None
        return b"&".join(arg.url_encode() for arg in self.query)


    def get_encoded_url(self):
        """
        :return bytes: The complete, encoded URL.
        """
        params = dict(
            scheme=self.scheme.encode("ascii"),
            host=self.get_encoded_host(),
            path=self.get_encoded_path(),
        )
        query = self.get_encoded_query()
        if query is None:
            params[b"query"] = b""
        else:
            params[b"query"] = b"?" + query
        if self.port is None:
            return b"%(scheme)s://%(host)s%(path)s%(query)s" % params
        params["port"] = self.port
        return b"%(scheme)s://%(host)s:%(port)d%(path)s%(query)s" % params


def request_details(**kw):
    return RequestDetails(**kw)

@attr.s
class RequestDetails(object):
    """
    Describe an AWS request in sufficient detail to sign and submit
    it.

    @ivar bytes region: The name of the region the request will be
        submitted to.

    @ivar bytes service: The name of the AWS service the request uses.

    @ivar bytes method: The HTTP method of the request.

    @ivar url_context: The details of the request URL.  An object
        returned by L{url_context}.

    @ivar twisted.web.http_headers.Headers headers: Any
        application-required HTTP headers for inclusion in the
        request.  This excludes headers like I{Content-Length} and
        I{Authorization}.

    @ivar twisted.web.iweb.IBodyProvider body_producer: An object
        which can produce the bytes which make up the request body.

    @ivar pmap metadata: Arbitrary key/value metadata to associate
        with the request.  See
        U{http://docs.aws.amazon.com/AmazonS3/latest/dev/UsingMetadata.html}
        (XXX: Is this S3-specific?)

    @ivar pmap amz_headers: AWS semantic key/value metadata to
        associate with the request.  For example, including the key
        u"storage-class" will tell AWS S3 to provide some particular
        alternate storage guarantees for an S3 object.

    @ivar unicode content_sha256: The hex digested sha256 of the bytes
        that C{body_producer} will produce.  If C{body_producer} will
        produce C{b""}, this must be hashed and included here.  If the
        hash cannot be computed, C{None} (which corresponds to a
        request with an unsigned payload - ie, with a payload
        unprotected from tampering by a signature).
    """
    region = attr.ib(validator=validators.instance_of(bytes))
    service = attr.ib(validator=validators.instance_of(bytes))
    method = attr.ib(validator=validators.instance_of(bytes))
    url_context = attr.ib()
    headers = attr.ib(
        default=attr.Factory(Headers),
        validator=validators.instance_of(Headers),
    )
    body_producer = attr.ib(
        default=None,
        validator=validators.optional(validators.provides(IBodyProducer)),
    )
    metadata = attr.ib(
        default=pmap(),
        convert=freeze,
        validator=validators.instance_of(PMap),
    )
    amz_headers = attr.ib(
        default=pmap(),
        convert=freeze,
        validator=validators.instance_of(PMap),
    )
    content_sha256 = attr.ib(
        default=None,
        validator=validators.optional(validators.instance_of(unicode)),
    )


def query(**kw):
    """
    Create a new AWS query model object.

    :param AWSCredentials credentials: The credentials to use for the
       query or ``None`` for an unauthenticated request.

    :param RequestDetails details: Stuff

    :param Cooperator cooperator: A cooperator to use for large
        uploads or ``None`` for the global cooperator (recommended).
    """
    return _Query(**kw)


@attr.s(frozen=True)
class _Query(object):
    """
    Representation of enough information to submit an AWS request.
    """
    _credentials = attr.ib()
    _details = attr.ib()
    _reactor = attr.ib(default=attr.Factory(lambda: namedAny("twisted.internet.reactor")))

    def _sign(self, instant, credentials, service, region, method, url_context, headers, content_sha256):
        """
        Sign this query using its built in credentials.
        """
        request = _auth_v4._CanonicalRequest.from_headers(
            method=method,
            url=url_context.get_encoded_path(),
            headers={k.lower(): vs for (k, vs) in headers.getAllRawHeaders()},
            headers_to_sign=(b"host", b"x-amz-date"),
            payload_hash=content_sha256,
        )

        return _auth_v4._make_authorization_header(
            region=region,
            service=service,
            canonical_request=request,
            credentials=credentials,
            instant=instant,
        )


    def _get_headers(
            self, instant,
            method, url_context, app_headers, body,
            metadata, amz_headers, content_sha256,
    ):
        """
        Build the list of headers needed in order to perform AWS operations.
        """
        headers = {
            "x-amz-date": _auth_v4.makeAMZDate(instant),
        }
        for key, value in metadata.iteritems():
            headers["x-amz-meta-" + key] = value
        for key, value in amz_headers.iteritems():
            headers["x-amz-" + key] = value
        if content_sha256 is None:
            content_sha256 = b"UNSIGNED-PAYLOAD"
        headers["x-amz-content-sha256"] = content_sha256

        # Before we check if the content type is set, let's see if we can set
        # it by guessing the the mimetype.
        content_types = app_headers.getRawHeaders(u"content-type", None)
        if content_types is not None:
            headers["content-type"] = content_types[0]
        return headers


    def submit(self, agent=None, receiver_factory=None, utcnow=None):
        """
        Send this request to AWS.

        @param IAgent agent: The agent to use to issue the request.

        @param receiver_factory: Backwards compatibility only.  The
            value is ignored.

        @param utcnow: A function like L{datetime.datetime.utcnow} to
            get the time as of the call.  This is used to provide a
            stable timestamp for signing purposes.

        @return: A L{twisted.internet.defer.Deferred} that fires with
            the response body (L{bytes}) on success or with a
            L{twisted.python.failure.Failure} on error.  Most
            AWS-originated errors are represented as
            L{twisted.web.error.Error} instances.
        """
        if utcnow is None:
            utcnow = datetime.utcnow

        method = self._details.method
        url_context = self._details.url_context
        headers = self._details.headers.copy()
        body_producer = self._details.body_producer

        if agent is None:
            agent = _get_agent(url_context.scheme, url_context.get_encoded_host(), self._reactor)
        instant = utcnow()

        extra_headers = self._get_headers(
            instant,
            method, url_context, headers, body_producer,
            self._details.metadata, self._details.amz_headers,
            self._details.content_sha256,
        )
        for k, v in extra_headers.iteritems():
            headers.setRawHeaders(k, [v])

        if not headers.hasHeader(u"host"):
            # XXX I'm not sure this is the right encoding for the
            # value in this context.  Headers.setRawHeaders would do
            # something different if we just gave it the unicode.
            headers.setRawHeaders(u"host", [url_context.get_encoded_host()])

        if self._credentials is not None:
            _log.info(
                u"Computing authorization from "
                u"{service} {region} {method} {url} {headers}",
                service=self._details.service,
                region=self._details.region,
                method=method,
                url=url_context.get_encoded_url(),
                headers=headers,
            )
            headers.setRawHeaders(u"authorization", [self._sign(
                instant,
                self._credentials,
                self._details.service,
                self._details.region,
                method,
                url_context,
                headers,
                self._details.content_sha256,
            )])

        url = url_context.get_encoded_url()
        _log.info(
            u"Submitting query: {method} {url} {headers}",
            method=method,
            url=url,
            headers=headers,
        )
        if body_producer is None:
            # Work around for https://twistedmatrix.com/trac/ticket/8984
            body_producer = FileBodyProducer(BytesIO(b""))

        d = agent.request(
            method,
            url,
            headers,
            body_producer,
        )
        d.addCallback(self._handle_response)
        return d

    def _handle_response(self, response):
        receiver = StreamingBodyReceiver()
        receiver.finished = d = Deferred()
        receiver.content_length = response.length
        response.deliverBody(receiver)
        d.addCallback(self._check_response, response)
        return d

    def _check_response(self, data, response):
        if response.code >= 400:
            return failure.Failure(TwistedWebError(response.code, response=data))
        return (response, data)


def _get_agent(scheme, host, reactor, contextFactory=None):
    if scheme == b"https":
        proxy_endpoint = os.environ.get("https_proxy")
        if proxy_endpoint:
            proxy_url = urlparse.urlparse(proxy_endpoint)
            endpoint = TCP4ClientEndpoint(reactor, proxy_url.hostname, proxy_url.port)
            return ProxyAgent(endpoint)
        else:
            if contextFactory is None:
                contextFactory = WebVerifyingContextFactory(host)
            return Agent(reactor, contextFactory)
    else:
        proxy_endpoint = os.environ.get("http_proxy")
        if proxy_endpoint:
            proxy_url = urlparse.urlparse(proxy_endpoint)
            endpoint = TCP4ClientEndpoint(reactor, proxy_url.hostname, proxy_url.port)
            return ProxyAgent(endpoint)
        else:
            return Agent(reactor)


class FakeClient(object):
    """
    XXX
    A fake client object for some degree of backwards compatability for
    code using the client attibute on BaseQuery to check url, status
    etc.
    """
    url = None
    status = None


class BaseQuery(object):

    def __init__(self, action=None, creds=None, endpoint=None, reactor=None,
        body_producer=None, receiver_factory=None):
        if not action:
            raise TypeError("The query requires an action parameter.")
        self.action = action
        self.creds = creds
        self.endpoint = endpoint
        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor
        self._client = None
        self.request_headers = None
        self.response_headers = None
        self.body_producer = body_producer
        self.receiver_factory = receiver_factory or StreamingBodyReceiver

    @property
    def client(self):
        if self._client is None:
            self._client_deprecation_warning()
            self._client = FakeClient()
        return self._client

    @client.setter
    def client(self, value):
        self._client_deprecation_warning()
        self._client = value

    def _client_deprecation_warning(self):
        warnings.warn('The client attribute on BaseQuery is deprecated and'
                      ' will go away in future release.', stacklevel=3)


    def get_page(self, url, *args, **kwds):
        """
        Define our own get_page method so that we can easily override the
        factory when we need to. This was copied from the following:
            * twisted.web.client.getPage
            * twisted.web.client._makeGetterFactory
        """
        contextFactory = None
        scheme, host, port, path = parse(url)
        data = kwds.get('postdata', None)
        self._method = method = kwds.get('method', 'GET')
        self.request_headers = self._headers(kwds.get('headers', {}))
        if (self.body_producer is None) and (data is not None):
            self.body_producer = FileBodyProducer(StringIO(data))
        if self.endpoint.ssl_hostname_verification:
            contextFactory = None
        else:
            contextFactory = WebClientContextFactory()
        agent = _get_agent(scheme, host, self.reactor, contextFactory)
        if scheme == "https":
            self.client.url = url
        d = agent.request(method, url, self.request_headers, self.body_producer)
        d.addCallback(self._handle_response)
        return d

    def _headers(self, headers_dict):
        """
        Convert dictionary of headers into twisted.web.client.Headers object.
        """
        return Headers(dict((k,[v]) for (k,v) in headers_dict.items()))

    def _unpack_headers(self, headers):
        """
        Unpack twisted.web.client.Headers object to dict. This is to provide
        backwards compatability.
        """
        return dict((k,v[0]) for (k,v) in headers.getAllRawHeaders())

    def get_request_headers(self, *args, **kwds):
        """
        A convenience method for obtaining the headers that were sent to the
        S3 server.

        The AWS S3 API depends upon setting headers. This method is provided as
        a convenience for debugging issues with the S3 communications.
        """
        if self.request_headers:
            return self._unpack_headers(self.request_headers)

    def _handle_response(self, response):
        """
        Handle the HTTP response by memoing the headers and then delivering
        bytes.
        """
        self.client.status = response.code
        self.response_headers = response.headers
        # XXX This workaround (which needs to be improved at that) for possible
        # bug in Twisted with new client:
        # http://twistedmatrix.com/trac/ticket/5476
        if self._method.upper() == 'HEAD' or response.code == NO_CONTENT:
            return succeed('')
        receiver = self.receiver_factory()
        receiver.finished = d = Deferred()
        receiver.content_length = response.length
        response.deliverBody(receiver)
        if response.code >= 400:
            d.addCallback(self._fail_response, response)
        return d

    def _fail_response(self, data, response):
       return fail(failure.Failure(
           TwistedWebError(response.code, response=data)))

    def get_response_headers(self, *args, **kwargs):
        """
        A convenience method for obtaining the headers that were sent from the
        S3 server.

        The AWS S3 API depends upon setting headers. This method is used by the
        head_object API call for getting a S3 object's metadata.
        """
        if self.response_headers:
            return self._unpack_headers(self.response_headers)
