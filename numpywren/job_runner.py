
import asyncio
import aiobotocore
import io
import numpy as np
import boto3
import concurrent.futures as fs
import time
import pywren
from numpywren import lambdapack as lp
import traceback
import multiprocessing as mp
from multiprocessing.dummy import Pool as ThreadPool
import logging
from pywren.serialize import serialize
import pickle
import gc
import os
import sys
import redis
import tracemalloc
import copy


REDIS_IP = os.environ.get("REDIS_IP", "")
REDIS_PASS = os.environ.get("REDIS_PASS", "")
REDIS_PORT = os.environ.get("REDIS_PORT", "9001")
REDIS_CLIENT = None

def mem():
   mem_bytes = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
   return mem_bytes/(1024.**3)

class LRUCache(object):
    def __init__(self, max_items=10):
        self.cache = {}
        self.key_order = []
        self.max_items = max_items

    def __setitem__(self, key, value):
        self.cache[key] = value
        self._mark(key)

    def __getitem__(self, key):
        try:
            value = self.cache[key]
        except KeyError:
            # Explicit reraise for better tracebacks
            raise KeyError
        self._mark(key)
        return value

    def __contains__(self, obj):
        return obj in self.cache

    def _mark(self, key):
        if key in self.key_order:
            self.key_order.remove(key)

        self.key_order.insert(0, key)
        if len(self.key_order) > self.max_items:
            remove = self.key_order[self.max_items]
            del self.cache[remove]
            self.key_order.remove(remove)

class LambdaPackExecutor(object):
    def __init__(self, program, loop, cache):
        self.read_executor = None
        self.write_executor = None
        self.compute_executor = None
        self.loop = loop
        self.program = program
        self.cache = cache
        self.block_ends= set()

    #@profile
    async def run(self, pc, computer=None, profile=True):
        pcs = [pc]
        for pc in pcs:
            print("STARTING INSTRUCTION ", pc)
            #print(self.program.inst_blocks[pc])
            t = time.time()
            node_status = self.program.get_node_status(pc)
            #print(node_status)
            self.program.set_max_pc(pc)
            inst_block = self.program.inst_blocks(pc)
            inst_block.start_time = time.time()
            instrs = inst_block.instrs
            next_pc = None
            if (len(instrs) != len(set(instrs))):
                raise Exception("Duplicate instruction in instruction stream")
            try:
                if (node_status == lp.NS.READY or node_status == lp.NS.RUNNING):
                    self.program.set_node_status(pc, lp.NS.RUNNING)
                    for instr in instrs:
                        instr.executor = computer
                        instr.cache = self.cache
                        #print("START: {0},  PC: {1}, time: {2}, pid: {3}".format(instr, pc, time.time(), os.getpid()))
                        if (instr.run):
                            e_str = "EXCEPTION: Same machine replay instruction: " + str(instr) + " PC: {0}, time: {1}, pid: {2}".format(pc, time.time(), os.getpid())
                            raise Exception(e_str)
                        flops = int(instr.get_flops())
                        read_size = instr.read_size
                        write_size = instr.write_size
                        self.program.incr_flops(flops)
                        self.program.incr_read(read_size)
                        self.program.incr_write(write_size)
                        res = await instr()
                        #print("END: {0}, PC: {1}, time: {2}, pid: {3}".format(instr, pc, time.time(), os.getpid()))
                        sys.stdout.flush()

                        instr.run = True
                        instr.cache = None
                        instr.executor = None
                    for instr in instrs:
                        instr.run = False
                        instr.result = None

                    next_pc = self.program.post_op(pc, lp.PS.SUCCESS, inst_block)
                elif (node_status == lp.NS.POST_OP):
                    #print("Re-running POST OP")
                    next_pc = self.program.post_op(pc, lp.PS.SUCCESS, inst_block)
                elif (node_status == lp.NS.NOT_READY):
                    raise
                    #print("THIS SHOULD NEVER HAPPEN OTHER THAN DURING TEST")
                    continue
                elif (node_status == lp.NS.FINISHED):
                    #print("HAHAH CAN'T TRICK ME... I'm just going to rage quit")
                    continue
                else:
                    raise Exception("Unknown status: {0}".format(node_status))
                if (next_pc != None):
                    pcs.append(next_pc)
            except fs._base.TimeoutError as e:
                self.program.decr_pool_size(1)
                self.program.decr_up(1)
                raise
            except RuntimeError as e:
                self.program.decr_pool_size(1)
                self.program.decr_up(1)
                raise
            except Exception as e:
                self.program.decr_pool_size(1)
                self.program.decr_up(1)
                traceback.print_exc()
                tb = traceback.format_exc()
                self.program.post_op(pc, lp.PS.EXCEPTION, inst_block, tb=tb)
                raise
        e = time.time()
        return pcs

def calculate_busy_time(rtimes):
    #pairs = [[(item[0], 1), (item[1], -1)] for sublist in rtimes for item in sublist]
    pairs = [[(item[0], 1), (item[1], -1)] for item in rtimes]
    events = sorted([item for sublist in pairs for item in sublist])
    running = 0
    wtimes = []
    current_start = 0
    for event in events:
        if running == 0 and event[1] == 1:
            current_start = event[0]
        if running == 1 and event[1] == -1:
            wtimes.append([current_start, event[0]])
        running += event[1]
    return wtimes


def reset_worker_visibility(worker_queue_url, worker_msg, interval, timeout):
    start_time = time.time()
    sqs_client = boto3.client('sqs')
    receipt_handle = worker_msg
    while True:
        res = sqs_client.change_message_visibility(VisibilityTimeout=timeout, QueueUrl=worker_queue_url, ReceiptHandle=receipt_handle)
        print("reset worker timeout at " + str(time.time() - start_time))
        time.sleep(interval)


#@profile
def lambdapack_run(program, task_id='', pipeline_width=5, msg_vis_timeout=60, cache_size=5, timeout=200, idle_timeout=60):
    if task_id != '':
        p = mp.Process(target=reset_worker_visibility,args=(program.worker_queue_url, task_id, 5, 10))
        p.start()
    program.incr_up(1)
    lambda_start = time.time()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    computer = fs.ThreadPoolExecutor(1)
    if (cache_size > 0):
        cache = LRUCache(max_items=cache_size)
    else:
        cache = None
    shared_state = {}
    shared_state["busy_workers"] = 0
    shared_state["done_workers"] = 0
    shared_state["pipeline_width"] = pipeline_width
    shared_state["running_times"] = []
    shared_state["last_busy_time"] = time.time()
    loop.create_task(check_program_state(program, loop, shared_state, timeout, idle_timeout))
    tasks = []
    for i in range(pipeline_width):
        # all the async tasks share 1 compute thread and a io cache
        coro = lambdapack_run_async(loop, program, computer, cache, i, shared_state=shared_state, msg_vis_timeout=msg_vis_timeout, timeout=timeout)
        tasks.append(loop.create_task(coro))
    #results = loop.run_until_complete(asyncio.gather(*tasks))
    loop.run_forever()
    print("loop end")
    loop.close()
    lambda_stop = time.time()
    if task_id != '':
        p.terminate()
        sqs_client = boto3.client("sqs")
        sqs_client.delete_message(QueueUrl=program.worker_queue_url, ReceiptHandle=task_id)
    program.decr_up(1)
    program.decr_pool_size(1)
    print(program.program_status())
    return {"up_time": [lambda_start, lambda_stop],
            "exec_time": calculate_busy_time(shared_state["running_times"])}

async def reset_msg_visibility(msg, queue_url, loop, timeout, lock):
    
    while(lock[0] == 1):
        try:
            receipt_handle = msg["ReceiptHandle"]
            pc = int(msg["Body"])
            sqs_client = boto3.client('sqs')
            res = sqs_client.change_message_visibility(VisibilityTimeout=timeout, QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            
            await asyncio.sleep(max(5, timeout-5))
        except Exception as e:
            print("PC: {0} Exception in reset msg vis ".format(pc) + str(e))
            await asyncio.sleep(10)
    pc = int(msg["Body"])
    print("Exiting msg visibility for {0}".format(pc))
    return 0

async def check_program_state(program, loop, shared_state, timeout, idle_timeout):
    start_time = time.time()
    last_check_status = time.time()
    shared_state['last_busy_time'] = time.time()
    while(True):
        if shared_state["busy_workers"] == 0:
            if time.time() - start_time > timeout:
                break
            if time.time() - shared_state["last_busy_time"] >  idle_timeout:
                break
        #TODO make this an s3 access as opposed to DD access since we don't *really need* atomicity here
        #TODO make this coroutine friendly
        if time.time() - last_check_status > 10:
          s = program.program_status()
          if(s != lp.PS.RUNNING):
             break
          last_check_status = time.time()
        await asyncio.sleep(1)
    print("Closing loop")
    loop.stop()

REDIS_CLIENT = None

#@profile
async def lambdapack_run_async(loop, program, computer, cache, i, shared_state, pipeline_width=1, msg_vis_timeout=60, timeout=200):
    shared_state["last_busy_time"] = time.time()
    #print("lambdapack run async started.")
    start_time = time.time()
    global REDIS_CLIENT
    #print("LAMBDAPACK_RUN_ASYNC")
    session = aiobotocore.get_session(loop=loop)
    # every pipelined worker gets its own copy of program so we don't step on eachothers toes!
    orig_program = program
    shared_state["busy_workers"] += 1
    if i > 0:
    	program = copy.deepcopy(orig_program)
    shared_state["busy_workers"] -= 1
    shared_state["last_busy_time"] = time.time()
    
    #serializer = serialize.SerializeIndependent()
    #print("serializer creation time: " + str(time.time() - start_time))
    #byte_string = serializer([program])[0][0]
    print("serialization time: " + str(time.time() - start_time))
    #program = pickle.loads(byte_string)
    #print("pickle time: " + str(time.time() - start_time))
    lmpk_executor = LambdaPackExecutor(program, loop, cache)
    #print("executor time: " + str(time.time() - start_time))
    running_times = shared_state['running_times']
    #last_message_time = time.time()
    if (REDIS_CLIENT == None):
      REDIS_CLIENT = redis.StrictRedis(REDIS_IP, port=REDIS_PORT, password=REDIS_PASS, db=0, socket_timeout=5)
    print("redis time: " + str(time.time() - start_time))
    redis_client = REDIS_CLIENT
    print("starting work loop " + str(i) + "  " + str(time.time() - start_time))
    shared_state["last_busy_time"] = time.time()
    try:
        while(True):
            current_time = time.time()
            if ((current_time - start_time) > timeout):
                print("Hit timeout...returning now")
                shared_state["done_workers"] += 1
                return
            await asyncio.sleep(1)
            # go from high priority -> low priority
            for queue_url in program.queue_urls[::-1]:
                async with session.create_client('sqs', use_ssl=False,  region_name='us-west-2') as sqs_client:
                    messages = await sqs_client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
                if ("Messages" not in messages):
                    continue
                else:
                    # note for loops in python leak scope so when this breaks
                    # messages = messages
                    # queue_url= messages
                    break
            if ("Messages" not in messages):
                print("No message is found at " + str(time.time() - start_time))
                #if time.time() - last_message_time > 10:
                #    return running_times
                continue
            print("Message is found at " + str(time.time() - start_time) + " for coro " + str(i))
            shared_state["busy_workers"] += 1
            redis_client.incr("{0}_busy".format(program.hash))
            #last_message_time = time.time()
            start_processing_time = time.time()
            msg = messages["Messages"][0]
            receipt_handle = msg["ReceiptHandle"]
            # if we don't finish in 75s count as a failure
            #res = sqs_client.change_message_visibility(VisibilityTimeout=1800, QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            pc = int(msg["Body"])
            redis_client.set(msg["MessageId"], str(time.time()))
            #print("creating lock")
            lock = [1]
            coro = reset_msg_visibility(msg, queue_url, loop, msg_vis_timeout, lock)
            loop.create_task(coro)
            redis_client.incr("{0}_{1}_start".format(program.hash, pc))
            print("processing start took " + str(time.time() - start_processing_time) + " for " + str(i))
            #snapshot_before = tracemalloc.take_snapshot()
            pcs = await lmpk_executor.run(pc, computer=computer)
            #snapshot_after = tracemalloc.take_snapshot()
            #top_stats = snapshot_after.compare_to(snapshot_before, 'lineno')
            #print("[ Top 100 differences for PC:{0} ]".format(pc))
            #for stat in top_stats[:10]:
            #   print(stat)

            print("processing end took " + str(time.time() - start_processing_time) + " for " + str(i))
            for pc in pcs:
                program.set_node_status(pc, lp.NS.FINISHED)
            async with session.create_client('sqs', use_ssl=False,  region_name='us-west-2') as sqs_client:
                #print("Job done...Deleting message for {0}".format(pcs[0]))
                lock[0] = 0
                await sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            '''
            sqs_client = boto3.client('sqs')
            sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            '''
            end_processing_time = time.time()
            running_times.append((start_processing_time, end_processing_time))
            print("processing whole took " + str(end_processing_time - start_processing_time) + " for " + str(i))
            redis_client.decr("{0}_busy".format(program.hash))
            shared_state["last_busy_time"] = time.time()
            current_time = time.time()
            shared_state["busy_workers"] -= 1
            if (current_time - start_time > timeout):
                return
            #    print("Hit timeout...returning now")
            #    shared_state["done_workers"] += 1
            #    return running_times
    except Exception as e:
        #print(e)
        traceback.print_exc()
        raise
    return

















