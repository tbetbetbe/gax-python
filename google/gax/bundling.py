# Copyright 2016, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Provides behavior that supports request bundling.

:func:`compute_bundle_id` is used generate ids linking API requests to the
appropriate bundles.

:class:`Event` is the result of scheduling a bundled api call.  It is a
decorated :class:`threading.Event`; its ``wait`` and ``is_set`` methods
are used wait for the bundle request to complete or determine if it has
been completed respectively.

:class:`Task` manages the sending of all the requests in a specific bundle.

:class:`Executor` has a ``schedule`` method that is used add bundled api calls
to a new or existing :class:`Task`.

"""

from __future__ import absolute_import

import collections
import copy
import logging
import threading

_LOG = logging.getLogger(__name__)


def _str_dotted_getattr(obj, name):
    """Expands extends getattr to allow dots in x to indicate nested objects.

    Args:
       obj: an object
       name: a name for a field in the object

    Returns:
       the value of named attribute

    Raises:
       AttributeError if the named attribute does not exist
    """
    if name.find('.') == -1:
        return getattr(obj, name)
    for part in name.split('.'):
        obj = getattr(obj, part)
    return str(obj) if obj is not None else None


def compute_bundle_id(obj, descriminator_fields):
    """Computes a bundle id from the descriminator fields of `obj`.

    descriminator_fields may include '.' as a separator, which is used to
    indicate object traversal.  This is meant to allow fields in the
    computed bundle_id.

    the id is a tuple computed by going through the descriminator fields in
    order and obtaining the str(value) object field (or nested object field)

    if any descriminator field cannot be found, ValueError is raised.

    Args:
      obj: an object
      descriminator_fields: a list of descriminator fields in the order to be
        to be used in the id

    Returns:
      tuple: computed as described above

    Raises:
      AttributeError: if any descriminator fields attribute does not exist
    """
    return tuple(_str_dotted_getattr(obj, x) for x in descriminator_fields)


_WARN_DEMUX_MISMATCH = ('cannot demultiplex the bundled response, got'
                        ' %d subresponses; want %d, each bundled request will'
                        ' receive all responses')


class Task(object):
    """Coordinates the execution of a single bundle."""

    def __init__(self, api_call, bundle_id, bundled_field, bundling_request,
                 subresponse_field=None):
        """Constructor.

        Args:
           api_call (callable[[object], object]): the func that is this tasks's
             API call
           bundle_id (tuple): the id of this bundle
           bundle_field (str): the field used to create the bundled request
           bundling_request (object): the request to pass as the arg to api_call
           subresponse_field (str): optional field used to demultiplex responses

        """
        self._api_call = api_call
        self._bundling_request = bundling_request
        self.bundle_id = bundle_id
        self.bundled_field = bundled_field
        self.subresponse_field = subresponse_field
        self._in_deque = collections.deque()
        self._event_deque = collections.deque()

    @property
    def message_count(self):
        """The number of bundled messages."""
        return sum(len(msgs) for msgs in self._in_deque)

    @property
    def message_bytesize(self):
        """The size of in bytes of the bundle messages."""
        return sum(len(str(m)) for msgs in self._in_deque for m in msgs)

    def run(self):
        """Call the task's func.

        The task's func will be called with the bundling requests func
        """
        if len(self._in_deque) == 0:
            return
        req = self._bundling_request
        setattr(req,
                self.bundled_field,
                [m for msgs in self._in_deque for m in msgs])

        subresponse_field = self.subresponse_field
        if subresponse_field:
            self._run_with_subresponses(req, subresponse_field)
        else:
            self._run_with_no_subresponse(req)

    def _run_with_no_subresponse(self, req):
        try:
            resp = self._api_call(req)
            for event in self._event_deque:
                event.result = resp
                event.set()
        except Exception as exc:  # pylint: disable=broad-except
            for event in self._event_deque:
                event.result = exc
                event.set()
        finally:
            self._in_deque.clear()
            self._event_deque.clear()

    def _run_with_subresponses(self, req, subresponse_field):
        try:
            resp = self._api_call(req)
            in_sizes = [len(msgs) for msgs in self._in_deque]
            all_subresponses = getattr(resp, subresponse_field)
            if len(all_subresponses) != sum(in_sizes):
                _LOG.warn(_WARN_DEMUX_MISMATCH, len(all_subresponses),
                          sum(in_sizes))
                for event in self._event_deque:
                    event.result = resp
                    event.set()
            else:
                start = 0
                for i, event in zip(in_sizes, self._event_deque):
                    next_copy = copy.copy(resp)
                    subresponses = all_subresponses[start:start + i]
                    setattr(next_copy, subresponse_field, subresponses)
                    start += i
                    event.result = next_copy
                    event.set()
        except Exception as exc:  # pylint: disable=broad-except
            for event in self._event_deque:
                event.result = exc
                event.set()
        finally:
            self._in_deque.clear()
            self._event_deque.clear()

    def extend(self, msgs):
        """Adds msgs to the tasks.

        Args:
           msgs: a iterable of messages that can be appended to the task's
            bundle_field

        Returns:
           an :class:`Event` that can be used to wait on the response
        """
        self._in_deque.append(msgs)
        event = self._event_for(msgs)
        self._event_deque.append(event)
        return event

    def _event_for(self, msgs):
        """Creates an Event that is set when the bundle with msgs is sent."""
        event = Event()
        event.canceller = self._canceller_for(msgs, event)
        return event

    def _canceller_for(self, msgs, event):
        """Obtains a cancellation function that removes msgs

`        The returned cancellation function returns ``True`` if all messages
        was removed successfully from the _in_deque, and false if it was not.


        Args:
           msgs (iterable): the messages to be cancelled

        Returns:
           (callable[[], boolean]): used to remove the messages from the
              _in_deque
        """

        def canceller():
            """Cancels submission of ``msgs`` as part of this bundle.

            Returns:
               ``False`` if any of messages had already been sent, otherwise
               ``True``
            """
            try:
                self._event_deque.remove(event)
                self._in_deque.remove(msgs)
                return True
            except ValueError:
                return False

        return canceller


TIMER_FACTORY = threading.Timer
"""A class with an interface similar to threading.Timer.

Defaults to threading.Timer.  This makes it easy to plug-in alternate
timer implementations."""


class Executor(object):
    """Organizes bundling for an api service that requires it."""
    # pylint: disable=too-few-public-methods

    def __init__(self, options):
        """Constructor.

        Args:
           options (gax.BundleOptions): configures strategy this instance
             uses when executing bundled functions.

        """
        self._options = options
        self._tasks = {}
        self._task_lock = threading.RLock()
        self._timer = None

    def schedule(self, api_call, bundle_id, bundle_desc, bundling_request):
        """Schedules bundle_desc of bundling_request as part of bundle_id.

        The returned value an :class:`Event` that

        * has a ``result`` attribute that will eventually be set to the result
          the api call
        * will be used to wait for the response
        * holds the canceller function for canceling this part of the bundle

        Args:
          api_call (callable[[object], object]): the scheduled API call
          bundle_id (str): identifies the bundle on which the API call should be
            made
          bundle_desc (gax.BundleDescriptor): describes the structure of the
            bundled call
          bundling_request (object): the request instance to use in the API call

        Returns:
           an :class:`Event`
        """
        bundle = self._bundle_for(api_call, bundle_id, bundle_desc,
                                  bundling_request)
        msgs = getattr(bundling_request, bundle_desc.bundled_field)
        event = bundle.extend(msgs)

        # Run the bundle if the count threshold was reached.
        count_threshold = self._options.message_count_threshold
        if count_threshold > 0 and bundle.message_count >= count_threshold:
            self._run_now(bundle.bundle_id)

        # Run the bundle if the size threshold was reached.
        size_threshold = self._options.message_bytesize_threshold
        if size_threshold > 0 and bundle.message_bytesize >= size_threshold:
            self._run_now(bundle.bundle_id)

        return event

    def _bundle_for(self, api_call, bundle_id, bundle_desc, bundling_request):
        with self._task_lock:
            bundle = self._tasks.get(bundle_id)
            if bundle is None:
                bundle = Task(api_call, bundle_id, bundle_desc.bundled_field,
                              bundling_request,
                              subresponse_field=bundle_desc.subresponse_field)
                delay_threshold = self._options.delay_threshold
                if delay_threshold > 0:
                    self._run_later(bundle, delay_threshold)
                self._tasks[bundle_id] = bundle
            return bundle

    def _run_later(self, bundle, delay_threshold):
        with self._task_lock:
            if self._timer is None:
                the_timer = TIMER_FACTORY(
                    delay_threshold,
                    self._run_now,
                    args=[bundle.bundle_id])
                the_timer.start()
                self._timer = the_timer

    def _run_now(self, bundle_id):
        with self._task_lock:
            if bundle_id in self._tasks:
                a_task = self._tasks.pop(bundle_id)
                a_task.run()


class Event(object):
    """Wraps a threading.Event, adding, canceller and result attributes."""

    def __init__(self):
        """Constructor.

        """
        self._event = threading.Event()
        self.result = None
        self.canceller = None

    def is_set(self):
        """Calls ``is_set`` on the decorated :class:`threading.Event`."""
        return self._event.is_set()

    def set(self):
        """Calls ``set`` on the decorated :class:`threading.Event`."""
        return self._event.set()

    def clear(self):
        """Calls ``clear`` on the decorated :class:`threading.Event`.

        Also resets the result if one has been set.
        """
        self.result = None
        return self._event.clear()

    def wait(self, timeout=None):
        """Calls ``wait`` on the decorated :class:`threading.Event`."""
        return self._event.wait(timeout=timeout)

    def cancel(self):
        """Invokes the cancellation function provided on construction."""
        if self.canceller:
            return self.canceller()
        else:
            return False