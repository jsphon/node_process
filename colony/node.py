import multiprocessing
import traceback
from multiprocessing import Process
from queue import Queue
from threading import Thread

from colony.observer import Observer, Observable
from colony.persistent_variable import PersistentVariable
from colony.utils.function_info import FunctionInfo
from colony.utils.logging import get_logger


class Graph(object):
    def __init__(self, name=None, logger=None):
        self.logger = logger or get_logger()
        self.nodes = []
        self.process = multiprocessing.current_process()
        self.is_alive = False
        self.name = name or ''

    def add(self, node_class, *args, **kwargs):
        if 'logger' not in kwargs:
            kwargs['logger'] = self.logger
        new_node = node_class(*args, **kwargs)
        self.nodes.append(new_node)
        return new_node

    def add_node(self, *args, **kwargs):
        result = self.add(Node, *args, **kwargs)
        return result

    def add_thread_node(self, *args, **kwargs):
        result = self.add(ThreadNode, *args, **kwargs)
        return result

    def add_process_node(self, *args, **kwargs):
        result = self.add(ProcessNode, *args, **kwargs)
        return result

    def start(self):
        self.logger.info('Graph "%s" starting', self.name)
        for node in self.nodes:
            if hasattr(node, 'start'):
                node.start()
        self.is_alive = True

    def stop(self):
        self.logger.info('Graph "%s" received stop signal', self.name)
        self.is_alive = False
        for node in self.nodes:
            if isinstance(node.worker, AsyncWorker):
                node.stop()

        for node in self.nodes:
            if isinstance(node.worker, SyncWorker):
                node.stop()


class OutputPort(Observable):
    def __init__(self):
        super(OutputPort, self).__init__()

    def connect_to(self, node):
        self.node = node

    def notify(self, data):
        self.notify_observers(data)


class PoisonPill(object):
    pass


class InputPort(Observer):
    def __init__(self, node=None):
        self.node = node

    def connect_to(self, node):
        self.node = node

    def notify(self, data):
        self.node.handle_input(data)


class ArgInputPort(InputPort):
    def __init__(self, idx=None, node=None):
        super(ArgInputPort, self).__init__(node=node)
        self.idx = idx

    def notify(self, data):
        self.node.handle_input(data, idx=self.idx)


class MappingArgInputPort(ArgInputPort):
    def notify(self, data):
        for x in data:
            self.node.handle_input(x, idx=self.idx)


class KwargInputPort(InputPort):
    def __init__(self, kwarg):
        super(KwargInputPort, self).__init__()
        self.kwarg = kwarg

    def notify(self, data):
        self.node.handle_input(data, kwarg=self.kwarg)


class BatchArgInputPort(ArgInputPort):
    def __init__(self, batch_size=1, idx=None, node=None):
        super(BatchArgInputPort, self).__init__(idx=idx, node=node)
        self.batch_size = batch_size

    def notify(self, data):
        for batch in self.chunks(data):
            self.node.handle_input(batch, idx=self.idx)

    def chunks(self, payload):
        """ Yield successive n-sized chunks from l.
        """
        for i in range(0, len(payload), self.batch_size):
            yield payload[i:i + self.batch_size]


class Worker(object):
    def __init__(self, node):
        self.node = node

        # Take a copy so that we don't need to access node later
        self.target_class = node.target_class
        self.target_class_args = node.target_class_args
        self.target_class_kwargs = node.target_class_kwargs

    def execute(self, *args, **kwargs):
        raise NotImplemented()

    def _handle_result(self, result):
        self.node.set_value(result)
        self.node.output_port.notify(result)

    def start(self):
        raise NotImplemented()

    def stop(self):
        raise NotImplemented()

    def _get_target_func(self):
        if self.node.target_func:
            return self.node.target_func
        else:
            target_instance = self.target_class(*self.target_class_args, **self.target_class_kwargs.copy())
            target_instance.logger = self.node.logger
            return target_instance.execute


class SyncWorker(Worker):
    def __init__(self, *args, **kwargs):
        super(SyncWorker, self).__init__(*args, **kwargs)
        self.target = None
        self.isStarted = False

    def execute(self, *args, **kwargs):
        try:
            if self.isStarted:
                result = self.target(*args, **kwargs)
                self._handle_result(result)
            else:
                raise Exception('SyncWorker.execute called, even though it is not started.')
        except Exception as e:
            msg = 'SyncWorker failed to execute: %s' % str(e)
            exc = traceback.format_exc()
            self.node.logger.error(msg)
            self.node.logger.error(exc)

    def start(self):
        self.target = self._get_target_func()
        self.isStarted = True

    def stop(self):
        self.target = None
        self.isStarted = False


class AsyncWorker(Worker):
    def __init__(self, node, async_class=Thread, num_threads=10):
        super(AsyncWorker, self).__init__(node)

        self.num_threads = num_threads
        self.async_class = async_class

        self.queue_class = None
        self.worker_queue = None
        self.result_queue = None
        self.worker_threads = None
        self.result_thread = None

    def start(self):

        queue_class = _get_queue_class(self.async_class)
        self.worker_queue = queue_class()
        self.result_queue = queue_class()
        self.worker_threads = [self.async_class(target=self._worker) for _ in range(self.num_threads)]
        for thread in self.worker_threads:
            thread.start()

        self.result_thread = Thread(target=self._result_handler)
        self.result_thread.start()

    def stop(self):
        # It's important to let the worker threads stop first,
        # so that they have put their results onto result_queue
        # before this method adds the poison pill to it
        for _ in self.worker_threads:
            self.worker_queue.put(PoisonPill())

        for thread in self.worker_threads:
            thread.join()

        self.result_queue.put(PoisonPill())
        self.result_thread.join()

    def join(self):
        self.worker_queue.join()
        self.result_queue.join()

    def execute(self, *args, **kwargs):
        self.worker_queue.put((args, kwargs))

    def _worker(self):
        target = self._get_target_func()
        while True:
            payload = self.worker_queue.get()
            if isinstance(payload, PoisonPill):
                self.worker_queue.task_done()
                return
            else:
                args, kwargs = payload
                try:
                    result = target(*args, **kwargs)
                except Exception as e:
                    self.node.logger.error('AsyncWorker failed to execute target %s:'%str(target), str(e))
                    self.node.logger.error(traceback.format_exc())
                else:
                    self.result_queue.put(result)
                    self.worker_queue.task_done()

    def _result_handler(self):
        while True:
            payload = self.result_queue.get()
            if isinstance(payload, PoisonPill):
                self.result_queue.task_done()
                return
            else:
                result = payload
                self._handle_result(result)
                self.result_queue.task_done()


class Node(object):
    def __init__(self,
                 target_func=None,
                 target_class=None,
                 target_class_args=None,
                 target_class_kwargs=None,
                 reactive_input_ports=None,
                 default_reactive_input_values=None,
                 node_args=None,
                 node_kwargs=None,
                 name=None,
                 node_worker_class=None,
                 node_worker_class_args=None,
                 node_worker_class_kwargs=None,
                 logger=None):

        self.target_func = target_func
        self.target_class = target_class
        self.target_class_args = target_class_args or []
        self.target_class_kwargs = target_class_kwargs or {}
        self.target_instance = None
        self.logger = logger or get_logger()

        if target_func and target_class is None:
            target_info = FunctionInfo(target_func)
            num_reactive_input_ports = target_info.num_args
        elif target_class:
            target_info = FunctionInfo(self.target_class.execute)
            # Subtract 1 to ignore the self argument
            num_reactive_input_ports = target_info.num_args - 1
        else:
            raise ValueError('Provide target_func OR target_class')

        if isinstance(reactive_input_ports, list):
            assert len(reactive_input_ports) == num_reactive_input_ports
            self.reactive_input_ports = reactive_input_ports
            for rip in reactive_input_ports:
                rip.connect_to(self)
        elif isinstance(reactive_input_ports, ArgInputPort):
            # Single Reactive Input Port
            self.reactive_input_ports = [reactive_input_ports]
            self.reactive_input_ports[0].idx = 0
            self.reactive_input_ports[0].connect_to(self)
        else:
            self.reactive_input_ports = []
            for i in range(num_reactive_input_ports):
                rip = ArgInputPort(i, self)
                self.reactive_input_ports.append(rip)

        if default_reactive_input_values:
            self.reactive_input_values = default_reactive_input_values
        else:
            self.reactive_input_values = [None] * len(self.reactive_input_ports)

        if node_args:
            if isinstance(node_args, Node):
                node_args = [node_args]
            for i, node_arg in enumerate(node_args):
                try:
                    node_arg.output_port.register_observer(self.reactive_input_ports[i])
                except Exception:
                    self.logger.error('Failed to register on reactive input port %i', i)
                    self.logger.error('target_func: %s', str(target_func))
                    self.logger.error('target_class: %s', str(target_class))
                    raise

        self.output_port = OutputPort()
        self.output_port.connect_to(self)
        self.name = name

        self.passive_input_ports = {}
        self.passive_input_values = {}
        for kwarg in target_info.kwargs:
            kwip = KwargInputPort(kwarg.name)
            kwip.connect_to(self)
            self.passive_input_ports[kwarg.name] = kwip
            self.passive_input_values[kwarg.name] = kwarg.default

        if node_kwargs:
            for kwarg, node_kwarg in node_kwargs.items():
                try:
                    node_kwarg.output_port.register_observer(self.passive_input_ports[kwarg])
                except KeyError:
                    self.logger.error('Is %s in %s' % (kwarg, self.passive_input_ports.keys()))
                    self.logger.error('Is it really a arg?')
                    raise

        self._value = None
        self.worker = self._build_node_worker(node_worker_class, node_worker_class_args, node_worker_class_kwargs)

    def _build_node_worker(self, node_worker_class, node_worker_class_args, node_worker_class_kwargs):
        node_worker_class = node_worker_class or SyncWorker
        args = node_worker_class_args or []
        kwargs = node_worker_class_kwargs or {}
        return node_worker_class(self, *args, **kwargs)

    def __repr__(self):
        class_name = self.__class__.__name__
        if self.name:
            return '<%s name="%s">' % (class_name, self.name)
        else:
            return '<%s>' % class_name

    def notify(self, data=None, port_idx=0):
        if data is None:
            self.handle_input(data, None, None)
        else:
            self.reactive_input_ports[port_idx].notify(data)

    def notify_items(self, lst, port_idx=0):
        port = self.reactive_input_ports[port_idx]
        for data in lst:
            port.notify(data)

    def get_value(self):
        return self._value

    def set_value(self, value):
        self._value = value

    def handle_input(self, data=None, idx=None, kwarg=None):

        if kwarg is not None:
            self.passive_input_values[kwarg] = data
            return

        if idx is not None:
            self.reactive_input_values[idx] = data

        try:
            self.worker.execute(*self.reactive_input_values, **self.passive_input_values)
        except Exception as e:
            self.logger.error('Failed to execute worker: %s', str(e))
            self.logger.error(traceback.format_exc())

    def handle_result(self, result):
        self.set_value(result)
        self.output_port.notify(result)

    def start(self):
        self.worker.start()

    def stop(self):
        self.worker.stop()


class PersistentNode(Node):
    """ This Node's value will persist between different instance lifetimes """

    def __init__(self, folder=None, *args, **kwargs):
        super(PersistentNode, self).__init__(*args, **kwargs)
        variable_name = self.name or get_temporary_filename()
        self.persistent_value = PersistentVariable(variable_name, folder=folder)
        self.persistent_value.refresh()

    def get_value(self):
        return self.persistent_value.get_value()

    def set_value(self, value):
        self.persistent_value.set_value(value)


class DictionaryNode(PersistentNode):
    def __init__(self, *args, **kwargs):
        super(DictionaryNode, self).__init__(target_func=self.remember_dict, *args, **kwargs)
        value = self.get_value()
        if not value:
            print('Setting to empty dict')
            self.set_value(dict())
        else:
            print('Value had existing value of %s' % value)

    def remember_dict(self, payload):
        action, data = payload
        if action == 'update':
            value = self.get_value()
            print('Updating %s with %s' % (value, data))
            value.update(data)
            return value
        elif action == 'delete':
            value = self.get_value()
            if not isinstance(data, (list, tuple, set)):
                data = (data,)
            for x in data:
                if x in value:
                    value.pop(x)
            return value
        else:
            raise ValueError('action %s not recognised' % action)


class AsyncNode(Node):
    def __init__(self, target_func=None, async_class=Thread, num_threads=10, *args, **kwargs):
        super(AsyncNode, self).__init__(
            target_func=target_func,
            node_worker_class=AsyncWorker,
            node_worker_class_args=(async_class,),
            node_worker_class_kwargs={'num_threads': num_threads},
            *args,
            **kwargs
        )


class ProcessNode(Node):
    def __init__(self, target_func=None, num_threads=10, *args, **kwargs):
        super(ProcessNode, self).__init__(
            target_func=target_func,
            node_worker_class=AsyncWorker,
            node_worker_class_args=(Process,),
            node_worker_class_kwargs={'num_threads': num_threads},
            *args,
            **kwargs
        )


class ThreadNode(Node):
    def __init__(self, target_func=None, num_threads=10, *args, **kwargs):
        super(ThreadNode, self).__init__(
            target_func=target_func,
            node_worker_class=AsyncWorker,
            node_worker_class_args=(Thread,),
            node_worker_class_kwargs={'num_threads': num_threads},
            *args,
            **kwargs
        )


def _get_queue_class(async_class):
    if async_class == Thread:
        return Queue
    elif async_class == multiprocessing.Process:
        return multiprocessing.JoinableQueue


class TargetClass(object):
    def execute(self, *args, **kwargs):
        pass


def get_temporary_filename():
    import os
    import tempfile
    return os.path.basename(tempfile.NamedTemporaryFile().name)


if __name__ == '__main__':
    class ExampleTargetClass(TargetClass):
        def __init__(self, c):
            print('setting data')
            self.data = set([c])

        def execute(self, x):
            self.data.add(x)
            return self.data


    n = Node(target_class=ExampleTargetClass, target_class_args=(0,))
    n.start()

    n.notify(1)
    print(n.get_value())

    n.notify(2)
    print(n.get_value())

    n.notify(3)
    print(n.get_value())
