# ------------------------------------------------------------------------------------------------
# Copyright (c) 2018 Microsoft Corporation
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# ------------------------------------------------------------------------------------------------

import json
import logging
import os
import random
import socket
import struct
import time
import uuid
from copy import copy, deepcopy
from typing import Iterable

import gym
import gym.envs.registration
import gym.spaces
import minerl.env.spaces
import numpy as np
from lxml import etree
from minerl.env import comms
from minerl.env.comms import retry
from minerl.env.malmo import InstanceManager

logger = logging.getLogger(__name__)

malmo_version = "0.37.0"
missions_dir = os.path.join(os.path.dirname(__file__), 'missions')



class EnvException(Exception):
    def __init__(self, message):
        super(EnvException, self).__init__(message)


class MissionInitException(Exception):
    def __init__(self, message):
        super(MissionInitException, self).__init__(message)


MAX_WAIT = 60 * 3


class MineRLEnv(gym.Env):
    """MineRL Env  open ai gym compatible environment API"""

    metadata = {'render.modes': []}
    
    def __init__(self, xml, observation_space, action_space, default_action=None):
        self.action_space = None
        self.observation_space = None
        self.default_action = default_action

        self.xml = None
        self.integratedServerPort = 0
        self.role = 0
        self.agent_count = 0
        self.resets = 0
        self.ns = '{http://ProjectMalmo.microsoft.com}'
        self.client_socket = None

        self.resync_period = 0
        self.turn_key = ""
        self.exp_uid = ""
        self.done = True
        self.synchronous = True
        self.step_options = None
        self.width = 0
        self.height = 0
        self.depth = 0

        self.xml_file = xml
        self.has_init = False
        self.instance = None

        self.init(observation_space, action_space)

    def init(self,  observation_space, action_space, exp_uid=None, episode=0,
             action_filter=None, resync=0, step_options=0):
        """"Initialize a Malmo environment.
            xml - the mission xml.
            port - the MalmoEnv service's port.
            server - the MalmoEnv service address. Default is localhost.
            role - the agent role (0..N-1) for missions with N agents. Defaults to 0.
            exp_uid - the experiment's unique identifier. Generated if not given.
            episode - the "reset" start count for experiment re-starts. Defaults to 0.
            action_filter - an optional list of valid actions to filter by. Defaults to simple commands.
            step_options - encodes withTurnKey and withInfo in step messages. Defaults to info included,
            turn if required.

            TODO: Allow for adding existing Malmo instances.
        """
        if self.instance == None:
            self.instance = InstanceManager.get_instance().__enter__()
        # Parse XML file
        with open(self.xml_file, 'r') as f:
            xml_text = f.read()
        xml = xml_text.replace('$(MISSIONS_DIR)', missions_dir)
        
        # Bootstrap the environment if it hasn't been.
        role = 0

        if not xml.startswith('<Mission'):
            i = xml.index("<Mission")
            if i == -1:
                raise EnvException("Mission xml must contain <Mission> tag.")
            xml = xml[i:]

        self.xml = etree.fromstring(xml)
        self.role = role
        if exp_uid is None:
            self.exp_uid = str(uuid.uuid4())
        else:
            self.exp_uid = exp_uid

        self.action_space = action_space
        self.observation_space = observation_space

        # Force single agent
        self.agent_count = 1
        turn_based = self.xml.find('.//' + self.ns + 'TurnBasedCommands') is not None
        if turn_based:
            raise NotImplementedError("Turn based or multi-agent environments not supported.")
        else:
            self.turn_key = ""

        # Unclear what step_options does.            
        if step_options is None:
            self.step_options = 0 if not turn_based else 2
        else:
            self.step_options = step_options
        
        self.done = True

        self.resync_period = resync
        self.resets = episode

        e = etree.fromstring("""<MissionInit xmlns="http://ProjectMalmo.microsoft.com" 
                                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
                                SchemaVersion="" PlatformVersion=""" + '\"' + malmo_version + '\"' +
                             """>
                                <ExperimentUID></ExperimentUID>
                                <ClientRole>0</ClientRole>
                                <ClientAgentConnection>
                                    <ClientIPAddress>127.0.0.1</ClientIPAddress>
                                    <ClientMissionControlPort>0</ClientMissionControlPort>
                                    <ClientCommandsPort>0</ClientCommandsPort>
                                    <AgentIPAddress>127.0.0.1</AgentIPAddress>
                                    <AgentMissionControlPort>0</AgentMissionControlPort>
                                    <AgentVideoPort>0</AgentVideoPort>
                                    <AgentDepthPort>0</AgentDepthPort>
                                    <AgentLuminancePort>0</AgentLuminancePort>
                                    <AgentObservationsPort>0</AgentObservationsPort>
                                    <AgentRewardsPort>0</AgentRewardsPort>
                                    <AgentColourMapPort>0</AgentColourMapPort>
                                    </ClientAgentConnection>
                                </MissionInit>""")
        e.insert(0, self.xml)
        self.xml = e
        self.xml.find(self.ns + 'ClientRole').text = str(self.role)
        self.xml.find(self.ns + 'ExperimentUID').text = self.exp_uid
        if self.role != 0 and self.agent_count > 1:
            e = etree.Element(self.ns + 'MinecraftServerConnection',
                              attrib={'address': self.instance.host,
                                      'port': str(0)
                                      })
            self.xml.insert(2, e)

        video_producers = self.xml.findall('.//' + self.ns + 'VideoProducer')
        assert len(video_producers) == self.agent_count
        video_producer = video_producers[self.role]
        # Todo: Deprecate width, height, and POV forcing.
        self.width = int(video_producer.find(self.ns + 'Width').text)
        self.height = int(video_producer.find(self.ns + 'Height').text)
        want_depth = video_producer.attrib["want_depth"]
        self.depth = 4 if want_depth is not None and (want_depth == "true" or want_depth == "1" or want_depth is True) else 3
        # print(etree.tostring(self.xml))

        self.has_init = True

    def _process_observation(self, pov, info):
        """
        Process observation into the proper dict space.
        """
        pov = np.frombuffer(pov, dtype=np.uint8)

        if pov is None or len(pov) == 0:
            pov = np.zeros((self.height, self.width, self.depth), dtype=np.uint8)
        else:
            pov = pov.reshape((self.height, self.width, self.depth))

        if info:
            info = json.loads(info)
        else:
            info = {}

        # Process Info: (HotFix until updated in Malmo.)
        if "inventory" in info  and "inventory" in self.observation_space.spaces:
            inventory_spaces = self.observation_space.spaces['inventory'].spaces

            items = inventory_spaces.keys()
            inventory_dict = {k: 0 for k in inventory_spaces}
            # TODO change to maalmo
            for stack in info['inventory']:
                if 'type' in stack and 'quantity' in stack:
                    try:
                        inventory_dict[stack['type']] += stack['quantity'] 
                    except ValueError:
                        continue
                    except KeyError:
                        # We only care to observe what was specified in the space.
                        continue
            info['inventory'] = inventory_dict
        elif  "inventory" in self.observation_space.spaces and not "inventory" in info:
            # logger.warning("No inventory found in malmo observation! Yielding empty inventory.")
            # logger.warning(info)
            pass

        obs_dict = {
            'pov': pov
        }


        # Todo: Make this logic dict recursive.
        for k in self.observation_space.spaces:
            if k is not 'pov':
                if not (k in  info):
                    correction = self.observation_space.spaces[k].sample()
                    if isinstance(self.observation_space.spaces[k], gym.spaces.Dict):
                        for k in correction:
                            correction[k] *= 0
                    info[k] = correction
                    # logger.warning("Missing observation {} in Malmo".format(k))
                
                obs_dict[k] = info[k]

        return obs_dict

    def _process_action(self, action_in) -> str:
        """
        Process the actions into a proper command.
        """
        action_in = deepcopy(action_in)
        action_str = []
        for act in action_in:
            # Process enums.
            if isinstance(self.action_space.spaces[act], minerl.env.spaces.Enum):
                if isinstance(action_in[act] , int):
                    action_in[act] = self.action_space.spaces[act].values[action_in[act]]
                else:
                    assert isinstance(action_in[act], str), "Enum action {} must be str or int".format(act)
                    assert action_in[act] in self.action_space.spaces[act].values, "Invalid value for enum action {}, {}".format(act, action_in[act])

            elif isinstance(self.action_space.spaces[act], gym.spaces.Box):
                subact = action_in[act]
                assert not isinstance(subact, str), "Box action {} is a string! It should be a ndarray: {}".format(act, subact)
                if isinstance(subact, np.ndarray):
                    subact = subact.flatten()
                
                if isinstance(subact, Iterable):
                    subact = " ".join(str(x) for x in subact)
    
                action_in[act] = subact

            action_str.append(
                "{} {}".format(act, str(action_in[act])))

        return "\n".join(action_str)


    @staticmethod
    def _hello(sock):
        comms.send_message(sock, ("<MalmoEnv" + malmo_version + "/>").encode())

    def reset(self):
        """gym api reset"""
        # Add support for existing instances.
        if not self.has_init:
            self.init()

        if self.resync_period > 0 and (self.resets + 1) % self.resync_period == 0:
            self.exit_resync()

        while not self.done:
            self.done = self._quit_episode()

            if not self.done:
                time.sleep(0.1)

        return self._start_up()

    @retry
    def _start_up(self):
        self.resets += 1
        if self.role != 0:
            self._find_server()
        if not self.client_socket:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            sock.connect((self.instance.host, self.instance.port))
            self._hello(sock)
            self.client_socket = sock  # Now retries will use connected socket.
        self._init_mission()
        self.done = False
        return self._peek_obs()

    def _peek_obs(self):
        obs = None
        start_time = time.time()
        if not self.done:
            logger.debug("Peeking the client.")
            peek_message = "<Peek/>"
            comms.send_message(self.client_socket, peek_message.encode())
            obs = comms.recv_message(self.client_socket)
            info = comms.recv_message(self.client_socket).decode('utf-8')

            
            reply = comms.recv_message(self.client_socket)
            done, = struct.unpack('!b', reply)
            self.done = done == 1
            if obs is None or len(obs) == 0:
                if time.time() - start_time > MAX_WAIT:
                    self.client_socket.close()
                    self.client_socket = None
                    raise MissionInitException('too long waiting for first observation')
                time.sleep(0.1)

        return self._process_observation(obs,info), {}

    def _quit_episode(self):
        comms.send_message(self.client_socket, "<Quit/>".encode())
        reply = comms.recv_message(self.client_socket)
        ok, = struct.unpack('!I', reply)
        return ok != 0

    def render(self):
        """gym api render"""
        pass

    def seed(self):
        print("WARNING: Seeds not supported yet.")


    def step(self, action):
        """gym api step"""
        obs = None
        reward = None
        info = None
        turn = True
        withturnkey = self.step_options < 2
        # print(withturnkey)
        withinfo = self.step_options == 0 or self.step_options == 2

        malmo_command =  self._process_action(action)
        
        if not self.done:
            step_message = "<Step" + str(self.step_options) + ">" + \
                           malmo_command + \
                           "</Step" + str(self.step_options) + " >"
            t0 = time.time()
            comms.send_message(self.client_socket, step_message.encode())
            # print("send action {}".format(time.time() - t0)); t0 = time.time()
            if withturnkey:
                comms.send_message(self.client_socket, self.turn_key.encode())
            obs = comms.recv_message(self.client_socket)
            # print("recieve obs {}".format(time.time() - t0)); t0 = time.time()

            reply = comms.recv_message(self.client_socket)
            reward, done, sent = struct.unpack('!dbb', reply)
            # print("recieve reward {}".format(time.time() - t0)); t0 = time.time()
            self.done = done == 1
            if withinfo:
                info = comms.recv_message(self.client_socket).decode('utf-8')
            
            out_obs = self._process_observation(obs, info)

            turn_key = comms.recv_message(self.client_socket).decode('utf-8') if withturnkey else ""
            # print("[" + str(self.role) + "] TK " + turn_key + " self.TK " + str(self.turn_key))
            if turn_key != "":
                if sent != 0:
                    turn = False
                # Done turns if: turn = self.turn_key == turn_key
                self.turn_key = turn_key
            else:
                turn = sent == 0

            # if (obs is None or len(obs) == 0) or turn:
                # time.sleep(0.1)
            # print("turnkeyprocessor {}".format(time.time() - t0)); t0 = time.time()
            # print("creating obs from buffer {}".format(time.time() - t0)); t0 = time.time()
        return out_obs, reward, self.done, {}

    def close(self):
        """gym api close"""
        try:
            # Purge last token from head node with <Close> message.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.instance.host, self.instance.port))
            self._hello(sock)

            comms.send_message(sock, ("<Close>" + self._get_token() + "</Close>").encode())
            reply = comms.recv_message(sock)
            ok, = struct.unpack('!I', reply)
            assert ok
            sock.close()
        except Exception as e:
            self._log_error(e)
        if self.client_socket:
            self.client_socket.close()
            self.client_socket = None

        if self.instance and self.instance.running:
            self.instance.kill()

    def reinit(self):
        """Use carefully to reset the episode count to 0."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.instance.host, self.instance.port))
        self._hello(sock)

        comms.send_message(sock, ("<Init>" + self._get_token() + "</Init>").encode())
        reply = comms.recv_message(sock)
        sock.close()
        ok, = struct.unpack('!I', reply)
        return ok != 0

    def status(self, head):
        """Get status from server.
        head - Ping the the head node if True.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if head:
            sock.connect((self.instance.host, self.instance.port))
        else:
            sock.connect((self.instance.host2, self.instance.port2))
        self._hello(sock)

        comms.send_message(sock, "<Status/>".encode())
        status = comms.recv_message(sock).decode('utf-8')
        sock.close()
        return status

    def exit(self):
        """Use carefully to cause the Minecraft service to exit (and hopefully restart).
        Likely to throw communication errors so wrap in exception handler.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.instance.host2, self.instance.port2))
        self._hello(sock)

        comms.send_message(sock, ("<Exit>" + self._get_token() + "</Exit>").encode())
        reply = comms.recv_message(sock)
        sock.close()
        ok, = struct.unpack('!I', reply)
        return ok != 0

    def resync(self):
        """make sure we can ping the head and assigned node.
        Possibly after an env.exit()"""
        success = 0
        for head in [True, False]:
            for _ in range(30):
                try:
                    self.status(head)
                    success += 1
                    break
                except Exception as e:
                    self._log_error(e)
                    time.sleep(10)

        if success != 2:
            raise EnvException("Failed to contact service" + (" head" if success == 0 else ""))

    def exit_resync(self):
        """Exit the current Minecraft and wait for new one to replace it."""
        print("********** exit & resync **********")
        try:
            if self.client_socket:
                self.client_socket.close()
                self.client_socket = None
            try:
                self.exit()
            except Exception as e:
                self._log_error(e)
            print("Pause for exit(s) ...")
            time.sleep(60)
        except (socket.error, ConnectionError):
            pass
        self.resync()

    def _log_error(self, exn):
        pass  # Keeping pylint happy

    def _find_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.instance.host, self.instance.port))
        self._hello(sock)

        start_time = time.time()
        port = 0
        while port == 0:
            comms.send_message(sock, ("<Find>" + self._get_token() + "</Find>").encode())
            reply = comms.recv_message(sock)
            port, = struct.unpack('!I', reply)
            if port == 0:
                if time.time() - start_time > MAX_WAIT:
                    if self.client_socket:
                        self.client_socket.close()
                        self.client_socket = None
                    raise MissionInitException('too long finding mission to join')
                time.sleep(1)
        sock.close()
        # print("Found mission integrated server port " + str(port))
        self.integratedServerPort = port
        e = self.xml.find(self.ns + 'MinecraftServerConnection')
        if e is not None:
            e.attrib['port'] = str(self.integratedServerPort)

    def _init_mission(self):
        ok = 0
        while ok != 1:
            xml = etree.tostring(self.xml)
            # syncticking always ;))))))))))))))))))))))))))))))))))))))))))))))))))))
            token = (self._get_token() + ":" + str(self.agent_count) + ":" + str(self.synchronous).lower()).encode()
            # print(xml.decode())
            comms.send_message(self.client_socket, xml)
            comms.send_message(self.client_socket, token)

            reply = comms.recv_message(self.client_socket)
            ok, = struct.unpack('!I', reply)
            self.turn_key = comms.recv_message(self.client_socket).decode('utf-8')
            if ok != 1:
                time.sleep(1)

    def _get_token(self):
        return self.exp_uid + ":" + str(self.role) + ":" + str(self.resets)


def make():
    return Env()


def register(id, **kwargs):
    # TODO create doc string based on registered envs
    return gym.envs.register(id, **kwargs)
