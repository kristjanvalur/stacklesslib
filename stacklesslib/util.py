#util.py
import sys
import stackless
import contextlib
import weakref
import collections
from . import main
from .errors import TimeoutError
from .base import atomic
from . import app

WaitTimeoutError = TimeoutError # backwards compatibility

class local(object):
    """Tasklet local storage.  Similar to threading.local"""
    def __init__(self):
        object.__getattribute__(self, "__dict__")["_tasklets"] = weakref.WeakKeyDictionary()

    def get_dict(self):
        d = object.__getattribute__(self, "__dict__")["_tasklets"]
        try:
            a = d[stackless.getcurrent()]
        except KeyError:
            a = {}
            d[stackless.getcurrent()] = a
        return a

    def __getattribute__(self, name):
        a = object.__getattribute__(self, "get_dict")()
        if name == "__dict__":
            return a
        elif name in a:
            return a[name]
        else:
            return object.__getattribute__(self, name)


    def __setattr__(self, name, value):
        a = object.__getattribute__(self, "get_dict")()
        a[name] = value

    def __delattr__(self, name):
        a = object.__getattribute__(self, "get_dict")()
        try:
            del a[name]
        except KeyError:
            raise AttributeError(name)


def channel_wait(chan, delay=None):
    """channel.receive with an optional timeout"""
    with timeout(delay, blocked=True):
        return chan.receive()

def send_throw(channel, exc, val=None, tb=None):
    """send exceptions over a channel.  Has the same semantics
       for exc, val and tb as the raise statement has.  Use this
       for backwards compatibility with versions of stackless that
       don't have the "send_throw" method on channels.
    """
    if hasattr(channel, "send_throw"):
        return channel.send_throw(exc, val, tb)
    #currently, channel.send_exception allows only (type, arg1, ...)
    #and can"t cope with tb
    if exc is None:
        if val is None:
            val = sys.exc_info()[1]
        exc = val.__class__
    elif val is None:
        if isinstance(type, exc):
            exc, val = exc, ()
        else:
            exc, val = exc.__class__, exc
    if not isinstance(val, tuple):
        val = val.args
    channel.send_exception(exc, *val)

def tasklet_throw(tasklet, exc, val=None, tb=None, immediate=True):
    """
    Throw an exception on a tasklet.  Emulates the new functionality
    for those versions that don't have tasklet.throw.
    Note, that delivery in those cases is always immediate.
    """
    if hasattr(tasklet, "throw"):
        return tasklet.throw(exc, val, tb, immediate)
    #currently, channel.send_exception allows only (type, arg1, ...)
    #and can"t cope with tb
    if exc is None:
        if val is None:
            val = sys.exc_info()[1]
        exc = val.__class__
    elif val is None:
        if isinstance(type, exc):
            exc, val = exc, ()
        else:
            exc, val = exc.__class__, exc
    if not isinstance(val, tuple):
        val = val.args
    tasklet.raise_exception(exc, *val)


class qchannel(stackless.channel):
    """
    A qchannel is like a channel except that it contains a queue, so that the
    sender never blocks.  If there isn't a blocked tasklet waiting for the data,
    the data is queued up internally.  The sender always continues.
    """
    def __init__(self):
        self.data_queue = collections.deque()
        self.preference = 1 #sender never blocks

    @property
    def balance(self):
        if self.data_queue:
            return len(self.data_queue)
        return super(qchannel, self).balance

    def send(self, data):
        sup = super(qchannel, self)
        with atomic():
            if sup.balance >= 0 and not sup.closing:
                self.data_queue.append((True, data))
            else:
                sup.send(data)

    def send_exception(self, exc, *args):
        self.send_throw(exc, args)

    def send_throw(self, exc, value=None, tb=None):
        """call with similar arguments as raise keyword"""
        sup = super(qchannel, self)
        with atomic():
            if sup.balance >= 0 and not sup.closing:
                self.data_queue.append((False, (exc, value, tb)))
            else:
                #deal with channel.send_exception signature
                send_throw(sup, exc, value, tb)

    def receive(self):
        with atomic():
            if not self.data_queue:
                return super(qchannel, self).receive()
            ok, data = self.data_queue.popleft()
            if ok:
                return data
            exc, value, tb = data
            try:
                raise exc, value, tb
            finally:
                tb = None

    #iterator protocol
    def send_sequence(self, sequence):
        for i in sequence:
            self.send(i)

    def __next__(self):
        return self.receive()

def tasklet_dispatcher(function):
    """
    A sipmle dispatcher which causes the function to start running on a new tasklet
    """
    stackless.tasklet(function)()

def call_async(function, dispatcher=tasklet_dispatcher, timeout=None,
               timeout_exception=WaitTimeoutError, onOrphaned=None):
    """Run the given function on a different tasklet and return the result.
       'dispatcher' must be a callable which, when called with with
       (func), causes asynchronous execution of the function to commence.
       If a result isn't received within an optional time limit, a 'timeout_exception' is raised.
       If the waiting tasklet is interrupted before the function returns, 'onOrphaned' is called.
    """
    chan = qchannel()
    done = [False] # avoid local binding in 'helper'
    def helper():
        """This helper marshals the result value over the channel"""
        try:
            try:
                try:
                    result = function()
                finally:
                    done[0] = True
            except Exception:
                send_throw(chan, *sys.exc_info())
            else:
                chan.send(result)
        except StopIteration:
            pass # The originator is no longer listening

    # submit the helper to the dispatcher
    dispatcher(helper)
    # wait for the result
    with atomic():
        try:
            return channel_wait(chan, timeout)
        finally:
            chan.close()
            if onOrphaned and not done[0]:
                onOrphaned()


# A timeout context manager
# First, a hidden inner exception that is not caught by normal exception handlers
class _InternalTimeout(BaseException):
    pass

# A class representing this instance.  Can be consulted to see if the
# error was thrown from here
class timeout_instance(object):
    def match(self, e):
        """returns true if the given object is the exception raised by this instance"""
        return isinstance(e, TimeoutError) and len(e.args) > 1 and e.args[1] is self
_null_timeout_instance = timeout_instance()

@contextlib.contextmanager
def timeout(delay, blocked=False):
    """
    A context manager which installes a timeout handler and will cause a
    TimeoutError to be raised on the tasklet if it is triggered.
    if "blocked" is true, then the timeout will only occur if the tasklet
    is blocked on a channel.
    """
    if delay is None or delay < 0:
        # The infinite timeout case
        yield _null_timeout_instance
        return

    # waiting_tasklet is used as a senty to show that the original
    # caller is still in the context manager
    waiting_tasklet = stackless.getcurrent()
    inst = timeout_instance() # Create a unique instance

    def old_callback():
        with atomic():
            if waiting_tasklet: # it is in the timeout context still
                if blocked and not waiting_tasklet.blocked:
                    return
                waiting_tasklet.raise_exception(_InternalTimeout, inst)

    def callback():
        with atomic():
            if waiting_tasklet: # it is in the timeout context still
                if blocked and not waiting_tasklet.blocked:
                    return # Don't timeout a non-blocked tasklets
                try:
                    waiting_tasklet.throw(_InternalTimeout(inst), immediate=False)
                except AttributeError:
                    # tasklet.throw is new. Fallback to raise_exception
                    # raise_exception is immediate, so we must call it on a tasklet
                    stackless.tasklet(old_callback)()

    def delayed_callback():
        # call the callback from a worker
        stackless.tasklet(callback)()

    with atomic():
        handle = app.event_queue.call_later(delay, callback)
        try:
            yield inst # Run the code
        except _InternalTimeout as e:
            # Check if it is _our_ exception instance:
            if e.args[0] is inst:
                # Turn it into the proper timeout
                handle = None
                raise TimeoutError("timed out after %ss"%(delay,), inst), None, sys.exc_info()[2]
            raise # it is someone else's error
        finally:
            waiting_tasklet = None
            if handle:
                handle.cancel()

def timeout_call(function, delay):
    """A call wrapper, returning (success, result), where "success" is true if there was no timeout"""
    try:
        with timeout(delay):
            return True, function()
    except TimeoutError as e:
        return False, e

def timeout_function(delay):
    """A decorator applying the given timeout to a function"""
    def helper(function):
        @contextlib.wraps(function)
        def wrapper(*args, **kwargs):
            with timeout(delay):
                return function(*args, **kwargs)
        return wrapper
    return helper
