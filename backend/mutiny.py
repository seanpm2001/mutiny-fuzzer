import datetime
import errno
import importlib
import os.path
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import argparse
import ssl
import scapy.all
from copy import deepcopy
from backend.proc_director import ProcDirector
from backend.fuzzer_types import Message, MessageCollection, Logger
from backend.packets import PROTO,IP
from mutiny_classes.mutiny_exceptions import *
from mutiny_classes.message_processor import MessageProcessorExtraParams, MessageProcessor
from mutiny_classes.exception_processor import ExceptionProcessor
from backend.fuzzer_data import FuzzerData
from backend.fuzzer_connection import FuzzerConnection
from backend.menu_functions import prompt, prompt_int, prompt_string, validate_number_range

class Mutiny(object):

    def __init__(self, args):
        self.fuzzer_data = FuzzerData()
        # read data in from .fuzzer file
        fuzzer_file_path = args.prepped_fuzz
        print("Reading in fuzzer data from %s..." % (fuzzer_file_path))
        self.fuzzer_data.read_from_file(fuzzer_file_path)
        self.target_host = args.target_host
        self.sleep_time = args.sleep_time
        self.dump_raw = args.dump_raw # test single seed, dump to dumpraw
        self.quiet = args.quiet # dont log the outputs 
        self.log_all = args.log_all if not self.quiet else False # kinda weird/redundant verbosity flags? 
        self.fuzzer_folder = os.path.abspath(os.path.dirname(fuzzer_file_path))
        self.output_data_folder_path = os.path.join("%s_%s" % (os.path.splitext(fuzzer_file_path)[0], "logs"), datetime.datetime.now().strftime("%Y-%m-%d,%H%M%S"))

        #Assign Lower/Upper bounds on test cases as needed
        if args.range:
            self.min_run_number, self.max_run_number = self._get_run_numbers_from_args(args.range)
        elif args.loop:
            self.seed_loop = validate_number_range(args.loop, flatten_list=True) 


        #TODO make it so logging message does not appear if reproducing (i.e. -r x-y cmdline arg is set)
        self.logger = None 
        if not self.quiet:
            print("Logging to %s" % (self.output_data_folder_path))
            self.logger = Logger(self.output_data_folder_path)

        if self.dump_raw:
            if not self.quiet:
                self.dump_dir = self.output_data_folder_path
            else:
                self.dump_dir = "dumpraw"
                try:
                    os.mkdir(self.dump_dir)
                except:
                    print("Unable to create dumpraw dir")
                    pass

        self.connection = None # connection to target


    def import_custom_processors(self):
        ######## Processor Setup ################
        # The processor just acts as a container #
        # class that will import custom versions #
        # messageProcessor/exceptionProessor/    #
        # monitor, if they are found in the      #
        # process_dir specified in the .fuzzer   #
        # file generated by fuzz_prep.py         #
        ##########################################

        if self.fuzzer_data.processor_directory == "default":
            # Default to fuzzer file folder
            self.fuzzer_data.processor_directory = self.fuzzer_folder
        else:
            # Make sure fuzzer file path is prepended
            self.fuzzer_data.processor_directory = os.path.join(self.fuzzer_folder, self.fuzzer_data.processor_directory)

        #Create class director, which import/overrides processors as appropriate
        proc_director = ProcDirector(self.fuzzer_data.processor_directory)

        ########## Launch child monitor thread
            ### monitor.task = spawned thread
            ### monitor.queue = enqueued exceptions
        self.monitor = proc_director.start_monitor(self.target_host, self.fuzzer_data.target_port)
        self.exception_processor = proc_director.exception_processor()
        self.message_processor = proc_director.message_processor()


    def fuzz(self):
        iteration = self.min_run_number - 1 if self.fuzzer_data.should_perform_test_run else self.min_run_number
        failure_count = 0
        loop_len = len(self.seed_loop) # if --loop
        is_paused = False

        while True:
            last_message_collection = deepcopy(self.fuzzer_data.message_collection)
            was_crash_detected = False
            if not is_paused and self.sleep_time > 0.0:
                print("\n** Sleeping for %.3f seconds **" % self.sleep_time)
                time.sleep(self.sleep_time)

            try:
                # Check for any exceptions from Monitor
                # Intentionally do this before and after a run in case we have back-to-back exceptions
                # (Example: Crash, then Pause, then Resume
                self.__raise_next_monitor_event_if_any(is_paused)
                
                if is_paused:
                    # Busy wait, might want to do something more clever with Condition or Event later
                    time.sleep(0.5)
                    continue

                try:
                    if self.dump_raw:
                        print("\n\nPerforming single raw dump case: %d" % self.dump_raw)
                        self._perform_run(seed=self.dump_raw)  
                    elif iteration == self.min_run_number - 1:
                        print("\n\nPerforming test run without fuzzing...")
                        self._perform_run(seed=-1 ) 
                    elif loop_len: 
                        print("\n\nFuzzing with seed %d" % (self.seed_loop[iteration%loop_len]))
                        self._perform_run(seed=self.seed_loop[iteration%loop_len]) 
                    else:
                        print("\n\nFuzzing with seed %d" % (iteration))
                        self._perform_run(seed=iteration) 
                    #if --quiet, (logger==None) => AttributeError
                    if self.log_all:
                        try:
                            self.logger.output_log(iteration, self.fuzzer_data.message_collection, "LogAll ")
                        except AttributeError:
                            pass 
                except Exception as e:
                    if self.log_all:
                        try:
                            self.logger.output_log(iteration, self.fuzzer_data.message_collection, "LogAll ")
                        except AttributeError:
                            pass
                    
                    if e.__class__ in MessageProcessorExceptions.all:
                        # If it's a MessageProcessorException, assume the MP raised it during the run
                        # Otherwise, let the MP know about the exception
                        raise e
                    else:
                        self.exception_processor.process_exception(e)
                        # Will not get here if processException raises another exception
                        print("Exception ignored: %s" % (repr(e)))
                
                # Check for any exceptions from Monitor
                # Intentionally do this before and after a run in case we have back-to-back exceptions
                # (Example: Crash, then Pause, then Resume
                self.__raise_next_monitor_event_if_any(is_paused)
            except PauseFuzzingException as e:
                print('Mutiny received a pause exception, pausing until monitor sends a resume...')
                is_paused = True

            except ResumeFuzzingException as e:
                if is_paused:
                    print('Mutiny received a resume exception, continuing to run.')
                    is_paused = False
                else:
                    print('Mutiny received a resume exception but wasn\'t paused, ignoring and continuing.')

            except LogCrashException as e:
                if failure_count == 0:
                    try:
                        print("Mutiny detected a crash")
                        self.logger.output_log(iteration, self.fuzzer_data.message_collection, str(e))
                    except AttributeError:  
                        pass   

                if self.log_all:
                    try:
                        self.logger.output_log(iteration, self.fuzzer_data.message_collection, "LogAll ")
                    except AttributeError:
                        pass

                failure_count = failure_count + 1
                was_crash_detected = True

            except AbortCurrentRunException as e:
                # Give up on the run early, but continue to the next test
                # This means the run didn't produce anything meaningful according to the processor
                print("Run aborted: %s" % (str(e)))
            
            except RetryCurrentRunException as e:
                # Same as AbortCurrentRun but retry the current test rather than skipping to next
                print("Retrying current run: %s" % (str(e)))
                # Slightly sketchy - a continue *should* just go to the top of the while without changing i
                continue
                
            except LogAndHaltException as e:
                if self.logger:
                    self.logger.output_log(iteration, self.fuzzer_data.message_collection, str(e))
                    print("Received LogAndHaltException, logging and halting")
                else:
                    print("Received LogAndHaltException, halting but not logging (quiet mode)")
                exit()
                
            except LogLastAndHaltException as e:
                if self.logger:
                    if iteration > self.min_run_number:
                        print("Received LogLastAndHaltException, logging last run and halting")
                        if self.min_run_number == self.max_run_number:
                            #in case only 1 case is run
                            self.logger.output_last_log(iteration, last_message_collection, str(e))
                            print("Logged case %d" % iteration)
                        else:
                            self.logger.output_last_log(iteratino-1, last_message_collection, str(e))
                    else:
                        print("Received LogLastAndHaltException, skipping logging (due to last run being a test run) and halting")
                else:
                    print("Received LogLastAndHaltException, halting but not logging (quiet mode)")
                exit()

            except HaltException as e:
                print("Received HaltException halting")
                exit()

            if was_crash_Detected:
                if failure_count < self.fuzzer_data.failure_threshold:
                    print("Failure %d of %d allowed for seed %d" % (failure_count, self.fuzzer_data.failure_threshold, iteration))
                    print("The test run didn't complete, continuing after %d seconds..." % (self.fuzzer_data.failure_timeout))
                    time.sleep(self.fuzzer_data.failure_timeout)
                else:
                    print("Failed %d times, moving to next test." % (failure_count))
                    failure_count = 0
                    i += 1
            else:
                i += 1
            
            # Stop if we have a maximum and have hit it
            if self.max_run_number >= 0 and i > self.max_run_number:
                exit()

            if self.dump_raw:
                exit()
            pass

    def _perform_run(self, seed: int = -1):
        '''
        Perform a fuzz run.  
        If seed is -1, don't perform fuzzing (test run)
        '''
        # Before doing anything, set up logger
        # Otherwise, if connection is refused, we'll log last, but it will be wrong
        if self.logger:
            self.logger.reset_for_new_run()
        
        # Call messageprocessor preconnect callback if it exists
        try:
            self.message_processor.pre_connect(seed, self.target_host, self.fuzzer_data.target_port) 
        except AttributeError:
            pass

        # create a connection to the target process
        self.connection = FuzzerConnection.connection(self.fuzzer_data.proto, self.target_host, self.fuzzer_data.target_port, self.fuzzer_data.source_ip, self.fuzzer_data.source_port, seed)

        iteration = 0   
        for iteration in range(0, len(self.fuzzer_data.message_collection.messages)):
            message = self.fuzzer_data.message_collection.messages[iteration]
            
            # Go ahead and revert any fuzzing or messageprocessor changes before proceeding
            message.reset_altered_message()

            if message.is_outbound():
                self._send_fuzz_session_message(iteration, message, seed)
            else: 
                self._receive_fuzz_session_message(iteration, message)

            if self.logger != None:  
                self.logger.set_highest_message_number(iteration)
            iteration += 1

        self.connection.close()

    def _receive_fuzz_session_message(self, iteration, message):
        # Receiving packet from server
        message_byte_array = message.get_altered_message()
        data = self.connection.receive_packet(len(message_byte_array), self.fuzzer_data.receive_timeout)
        self.message_processor.post_receive_process(data, MessageProcessorExtraParams(iteration, -1, False, [message_byte_array], [data]))

        if self.debug:
            print("\tReceived: %s" % (response))
        if data == message_byte_array:
            print("\tReceived expected response")
        if self.logger: 
            self.logger.set_received_message_data(iteration, data)
        if self.dump_raw:
            loc = os.path.join(self.dump_dir, "%d-inbound-seed-%d"%(iteration, self.dump_raw))
            with open(loc,"wb") as f:
                f.write(repr(str(data))[1:-1])

    def _send_fuzz_session_message(self, iteration, message, seed):
        # Primarily used for deciding how to handle preFuzz/preSend callbacks
        message_has_subcomponents = len(message.subcomponents) > 1

        # Get original subcomponents for outbound callback only once
        original_subcomponents = [subcomponent.get_original_byte_array() for subcomponent in message.subcomponents]
        
        if message_has_subcomponents:
            # For message with subcomponents, call prefuzz on fuzzed subcomponents
            for j in range(0, len(message.subcomponents)):
                subcomponent = message.subcomponents[j] 
                # Note: we WANT to fetch subcomponents every time on purpose
                # This way, if user alters subcomponent[0], it's reflected when
                # we call the function for subcomponent[1], etc
                actual_subcomponents = [subcomponent.get_altered_byte_array() for subcomponent in message.subcomponents]
                pre_fuzz = self.message_processor.pre_fuzz_subcomponent_process(subcomponent.get_altered_byte_array(), MessageProcessorExtraParams(iteration, j, subcomponent.is_fuzzed, original_subcomponents, actual_subcomponents))
                subcomponent.set_altered_byte_array(pre_fuzz)
        else:
            # If no subcomponents, call prefuzz on ENTIRE message
            actual_subcomponents = [subcomponent.get_altered_byte_array() for subcomponent in message.subcomponents]
            pre_fuzz = self.message_processor.pre_fuzz_process(actual_subcomponents[0], MessageProcessorExtraParams(iteration, -1, message.is_fuzzed, original_subcomponents, actual_subcomponents))
            message.subcomponents[0].set_altered_byte_array(pre_fuzz)

        # Skip fuzzing for seed == -1
        if seed > -1:
            # Now run the fuzzer for each fuzzed subcomponent
            self._fuzz_subcomponents(message, seed)
        
        # Fuzzing has now been done if this message is fuzzed
        # Always call preSend() regardless for subcomponents if there are any
        if message_has_subcomponents:
            for j in range(0, len(message.subcomponents)):
                subcomponent = message.subcomponents[j] 
                # See preFuzz above - we ALWAYS regather this to catch any updates between
                # callbacks from the user
                actual_subcomponents = [subcomponent.get_altered_byte_array() for subcomponent in message.subcomponents]
                pre_send = self.message_processor.pre_send_subcomponent_process(subcomponent.get_altered_byte_array(), MessageProcessorExtraParams(iteration, j, subcomponent.is_fuzzed, original_subcomponents, actual_subcomponents))
                subcomponent.set_altered_byte_array(pre_send)
        # Always let the user make any final modifications pre-send, fuzzed or not
        actual_subcomponents = [subcomponent.get_altered_byte_array() for subcomponent in message.subcomponents]
        byte_array_to_send = self.message_processor.pre_send_process(message.get_altered_message(), MessageProcessorExtraParams(iteration, -1, message.is_fuzzed, original_subcomponents, actual_subcomponents))

        if self.dump_raw:
            loc = os.path.join(self.dump_dir,"%d-outbound-seed-%d"%(iteration, self.dump_raw))
            if message.is_fuzzed:
                loc += "-fuzzed"
            with open(loc, "wb") as f:
                f.write(repr(str(byte_array_to_send))[1:-1])

        self.connection.send_packet(byte_array_to_send, self.fuzzer_data.receive_timeout)

        if self.debug:
            print("\tSent: %s" % (byte_array_to_send))
            print("\tRaw Bytes: %s" % (Message.serialize_byte_array(byte_array_to_send)))


    def _fuzz_subcomponents(message, seed):
        '''
        iterates through each subcomponent in a message and uses radamsa to generate fuzzed
        versions of each subcomponent if its .isFuzzed is set to True
        '''
        for subcomponent in message.subcomponents:
            if subcomponent.is_fuzzed:
                radamsa = subprocess.Popen([self.radamsa, "--seed", str(seed)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                byte_array = subcomponent.get_altered_byte_array()
                (fuzzed_byte_array, error_output) = radamsa.communicate(input=byte_array)
                fuzzed_byte_array = bytearray(fuzzed_byte_array)
                subcomponent.set_altered_byte_array(fuzzed_byte_array)


    def _raise_next_monitor_event_if_any(self, is_paused):
        # Check the monitor queue for exceptions generated during run
        if not self.monitor.queue.empty():
            print('Monitor event detected')
            exception = self.monitor.queue.get()
            
            if is_paused:
                if isinstance(exception, PauseFuzzingException):
                    # Duplicate pauses are fine, a no-op though
                    pass
                elif not isinstance(exception, ResumeFuzzingException):
                    # Any other exception besides resume after pause makes no sense
                    print(f'Received exception while Mutiny was paused, can\'t handle properly:')
                    print(repr(exception))
                    print('Exception will be ignored and discarded.')
                    return
            raise exception

    def _get_run_numbers_from_args(self, str_args):
    # Set MIN_RUN_NUMBER and MAX_RUN_NUMBER when provided
    # by the user below
        if "-" in str_args:
            test_numbers = str_args.split("-")
            if len(test_numbers) == 2:
                if len(test_numbers[1]): #e.g. str_args="1-50"
                    # cant have min > max
                    if (int(test_numbers[0]) > int(test_numbers[1])):
                        sys.exit("Invalid test range given: %s" % str_args)
                    return (int(test_numbers[0]), int(test_numbers[1]))
                else:                   #e.g. str_args="3-" (equiv. of --skip-to)
                    return (int(test_numbers[0]),-1)
            else: #e.g. str_args="1-2-3-5.." 
                sys.exit("Invalid test range given: %s" % str_args)
        else:
            # If they pass a non-int, allow this to bomb out
            return (int(str_args),int(str_args)) 
