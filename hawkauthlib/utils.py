# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
# pylint: disable=C0103
"""
Low-level utility functions for hawkauthlib.
"""

import sys
import re
import functools
import base64

import webob

requests = None
try:
    import requests
except ImportError:   # pragma: nocover
    pass


if sys.version_info > (3,):  # pragma: nocover

    bytes = bytes  # pylint: disable=W0622

    def iteritems(d):
        """Efficiently iterate over dict items."""
        return d.items()

    def b64encode(data):
        """Base64-encode bytes data into a native string."""
        return base64.b64encode(data).decode("ascii")

else:  # pragma: nocover

    bytes = str    # pylint: disable=W0622

    def iteritems(d):  # NOQA
        """Efficiently iterate over dict items."""
        return d.iteritems()

    def b64encode(data):  # NOQA
        """Base64-encode bytes data into a native string."""
        return base64.b64encode(data)


# Regular expression matching a single param in the HTTP_AUTHORIZATION header.
# This is basically <name>=<value> where <value> can be an unquoted token,
# an empty quoted string, or a quoted string where the ending quote is *not*
# preceded by a backslash.
_AUTH_PARAM_RE = r'([a-zA-Z0-9_\-]+)=(([a-zA-Z0-9_\-]+)|("")|(".*[^\\]"))'
_AUTH_PARAM_RE = re.compile(r"^\s*" + _AUTH_PARAM_RE + r"\s*$")

# Regular expression matching an unescaped quote character.
_UNESC_QUOTE_RE = r'(^")|([^\\]")'
_UNESC_QUOTE_RE = re.compile(_UNESC_QUOTE_RE)

# Regular expression matching a backslash-escaped characer.
_ESCAPED_CHAR = re.compile(r"\\.")


def parse_authz_header(request, *default):
    """Parse the authorization header into an identity dict.

    This function can be used to extract the Authorization header from a
    request and parse it into a dict of its constituent parameters.  The
    auth scheme name will be included under the key "scheme", and any other
    auth params will appear as keys in the dictionary.

    For example, given the following auth header value:

        'Digest realm="Sync" userame=user1 response="123456"'

    This function will return the following dict:

        {"scheme": "Digest", realm: "Sync",
         "username": "user1", "response": "123456"}

    """
    # This outer try-except catches ValueError and
    # turns it into return-default if necessary.
    try:
        # Grab the auth header from the request, if any.
        authz = request.environ.get("HTTP_AUTHORIZATION")
        if authz is None:
            raise ValueError("Missing auth parameters")
        scheme, kvpairs_str = authz.split(None, 1)
        # Split the parameters string into individual key=value pairs.
        # In the simple case we can just split by commas to get each pair.
        # Unfortunately this will break if one of the values contains a comma.
        # So if we find a component that isn't a well-formed key=value pair,
        # then we stitch bits back onto the end of it until it is.
        kvpairs = []
        if kvpairs_str:
            for kvpair in kvpairs_str.split(","):
                if not kvpairs or _AUTH_PARAM_RE.match(kvpairs[-1]):
                    kvpairs.append(kvpair)
                else:
                    kvpairs[-1] = kvpairs[-1] + "," + kvpair
            if not _AUTH_PARAM_RE.match(kvpairs[-1]):
                raise ValueError('Malformed auth parameters')
        # Now we can just split by the equal-sign to get each key and value.
        params = {"scheme": scheme}
        for kvpair in kvpairs:
            (key, value) = kvpair.strip().split("=", 1)
            # For quoted strings, remove quotes and backslash-escapes.
            if value.startswith('"'):
                value = value[1:-1]
                if _UNESC_QUOTE_RE.search(value):
                    raise ValueError("Unescaped quote in quoted-string")
                value = _ESCAPED_CHAR.sub(lambda m: m.group(0)[1], value)
            params[key] = value
        return params
    except ValueError:
        if default:
            return default[0]
        raise


def get_signature(request, key, params, algorithm=None):
    """Calculate the Hawk signature for the given request.

    This function calculates the Hawk signature for the given request and
    returns it as a string.

    The "params" parameter must contain all necessary Hawk signature parameters,
    including the payload hash if in use.
    """
    if algorithm is None:
        algorithm = "sha256"
    sigstr = utils.get_normalized_request_string(request, params)
    # The spec mandates that ids and keys must be ascii.
    # It's therefore safe to encode like this before doing the signature.
    sigstr = sigstr.encode("ascii")
    if not isinstance(key, utils.bytes):
        key = key.encode("ascii")
    hashmod = ALGORITHMS[algorithm]
    return utils.b64encode(hmac.new(key, sigstr, hashmod).digest())


def get_normalized_request_string(request, params=None, server_hash=None):
    """Get the string to be signed for Hawk access authentication.

    This method takes a WebOb Request object and optional server generated
    hash, returns the data that should be signed for Hawk access
    authentication of that request, a.k.a the "normalized request string".

    If the "params" parameter is not None, it is assumed to be a pre-parsed
    dict of Hawk parameters as one might find in the Authorization header.  If
    it is missing or None then the Authorization header from the request will
    be parsed to determine the necessary parameters.
    """
    if params is None:
        params = parse_authz_header(request, {})
    bits = []
    bits.append("hawk.1.header")
    bits.append(params["ts"])
    bits.append(params["nonce"])
    bits.append(request.method.upper())
    bits.append(request.path_qs)
    try:
        host, port = request.host.rsplit(":", 1)
    except ValueError:
        host = request.host
        if request.scheme == "http":
            port = "80"
        elif request.scheme == "https":
            port = "443"
        else:
            msg = "Unknown scheme %r has no default port" % (request.scheme,)
            raise ValueError(msg)
    bits.append(host.lower())
    bits.append(port)
    # In many cases, checking the MAC first is faster than calculating the payload hash
    # https://github.com/mozilla/hawk/blob/main/API.md
    if server_hash is None:
        bits.append(params.get("hash", ""))
    else:
        bits.append(server_hash)
    bits.append(params.get("ext", ""))
    bits.append("")     # to get the trailing newline
    return "\n".join(bits)


def get_normalized_payload_string(request):
    """Get the string to be hashed for Hawk payload verification.

    This function takes a WebOb Request object and returns the data that
    should be hashed for Hawk payload verification, a.k.a the "hash" parameter
    in the normalized request string.
    """
    # The Hawk spec mandates that we hash the body as UTF-8-encoded text,
    # so accessing it as `text` here is legitimate.
    if not request.text:
        return None

    try:
        content_type = request.content_type.split(';')[0].strip().lower()
    except ValueError:
        msg = "Could not derive the compliant Content-Type value from header value %s" % (request.content_type,)
        raise ValueError(msg)

    bits = []
    bits.append("hawk.1.payload")
    bits.append(content_type)
    bits.append(request.text)
    bits.append("")     # to get the trailing newline

    return "\n".join(bits)


def strings_differ(string1, string2):
    """Check whether two strings differ while avoiding timing attacks.

    This function returns True if the given strings differ and False
    if they are equal.  It's careful not to leak information about *where*
    they differ as a result of its running time, which can be very important
    to avoid certain timing-related crypto attacks:

        http://seb.dbzteam.org/crypto/python-oauth-timing-hmac.pdf

    """
    if len(string1) != len(string2):
        return True
    invalid_bits = 0
    for a, b in zip(string1, string2):
        invalid_bits += ord(a) ^ ord(b)
    return invalid_bits != 0


def normalize_request_object(func):
    """Decorator to normalize request into a WebOb request object.

    This decorator can be applied to any function taking a request object
    as its first argument, and will transparently convert other types of
    request object into a webob.Request instance.  Currently supported
    types for the request object are:

        * webob.Request objects
        * requests.Request objects
        * WSGI environ dicts
        * bytestrings containing request data
        * file-like objects containing request data

    If the input request object is mutable, then any changes that the wrapped
    function makes to the request headers will be written back to it at exit.
    """
    @functools.wraps(func)
    def wrapped_func(request, *args, **kwds):
        orig_request = request
        # Convert the incoming request object into a webob.Request.
        if isinstance(orig_request, webob.Request):
            pass
        # A requests.PreparedRequest object?
        elif requests and isinstance(orig_request, requests.PreparedRequest):
            # Copy over only the details needed for the signature.
            # WebOb doesn't code well with bytes header names,
            # so we have to be a little careful.
            request = webob.Request.blank(orig_request.url)
            request.method = orig_request.method
            for k, v in iteritems(orig_request.headers):
                if not isinstance(k, str):
                    k = k.decode('ascii')  # pragma: nocover
                request.headers[k] = v
        # A WSGI environ dict?
        elif isinstance(orig_request, dict):
            request = webob.Request(orig_request)
        # A bytestring?
        elif isinstance(orig_request, bytes):
            request = webob.Request.from_bytes(orig_request)
        # A file-like object?
        elif all(hasattr(orig_request, attr) for attr in ("read", "readline")):
            request = webob.Request.from_file(orig_request)

        # The wrapped function might modify headers.
        # Write them back if the original request object is mutable.
        try:
            return func(request, *args, **kwds)
        finally:
            if requests and isinstance(orig_request, requests.PreparedRequest):
                orig_request.headers.update(request.headers)

    return wrapped_func
