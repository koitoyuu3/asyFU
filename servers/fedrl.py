"""
A federated server with a reinforcement learning agent.
This federated server uses reinforcement learning
to tune the number of local aggregations on edge servers.
"""

import logging
import asyncio
import sys

from config import Config
from servers import FedAvgCrossSiloServer
import servers

FLServer = FedAvgCrossSiloServer
if Config().rl:
    from stable_baselines3.common.env_checker import check_env
    from rl_envs import FLEnv

    # The central server of FL
    FLServer = {
        "fedavg_cross_silo": servers.fedavg_cs.FedAvgCrossSiloServer
    }[Config().rl.fl_server]


class FedRLServer(FLServer):
    """Federated server using RL."""
    def __init__(self):
        super().__init__()

        self.rl_env = FLEnv(self)

        self.rl_episode = 0
        self.rl_tuned_para_value = None
        self.rl_state = None
        self.is_rl_tuned_para_got = False
        self.is_rl_episode_done = False

        # An RL agent waits for the event that the tuned parameter
        # is passed from RL environment
        self.rl_tuned_para_got = asyncio.Event()

        # An RL agent waits for the event that RL environment is reset to aviod
        # directly starting a new time step after the previous episode ends
        self.new_episode_begin = asyncio.Event()

    def configure(self):
        """
        Booting the RL agent and the FL server
        """
        logging.info('Configuring a RL agent and a %s server...',
                     Config().rl.fl_server)
        logging.info(
            "This RL agent will tune the number of aggregations on edge servers."
        )

        total_episodes = Config().rl.episodes
        target_reward = Config().rl.target_reward

        if target_reward:
            logging.info('RL Training: %s episodes or %s%% reward\n',
                         total_episodes, 100 * target_reward)
        else:
            logging.info('RL Training: %s episodes\n', total_episodes)

    def start_clients(self, as_server=False):
        """Start all clients and RL training."""
        super().start_clients(as_server)

        # The starting point of RL training
        # Run RL training as a coroutine
        if not as_server:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(asyncio.gather(self.start_rl()))

    def start_rl(self):
        """The starting point of RL training."""
        # Test the environment of reinforcement learning.
        #self.check_with_sb3_env_checker(self.rl_env)
        self.try_a_random_agent(self.rl_env)

    def reset_rl_env(self):
        """Reset the RL environment at the beginning of each episode."""
        # The number of finished FL training round
        self.current_round = 0

        self.is_rl_episode_done = False

        self.rl_episode += 1
        logging.info('\nRL Agent: starting episode %s...', self.rl_episode)

        # Configure the FL central server
        super().configure()

        # starting time of a gloabl training round
        self.round_start_time = 0

    async def wrap_up(self):
        """Wrapping up when one round of FL training is done."""
        # Get the RL state
        # Use accuracy as state for now
        self.rl_state = self.accuracy

        target_accuracy = Config().training.target_accuracy

        if target_accuracy and self.accuracy >= target_accuracy:
            logging.info('Target accuracy of FL reached.')
            self.is_rl_episode_done = True

        if self.current_round >= Config().training.rounds:
            logging.info('Target number of FL training rounds reached.')
            self.is_rl_episode_done = True

        # Pass the RL state to the RL env
        self.rl_env.get_state(self.rl_state, self.is_rl_episode_done)

        # Give RL env some time to finish step() before FL select clients to start next round
        await self.rl_env.step_done.wait()
        self.rl_env.step_done.clear()

        if self.is_rl_episode_done:
            await self.wrap_up_an_episode()

    async def wrap_up_server_response(self, server_response):
        """Wrap up generating the server response with any additional information."""
        await self.update_rl_tuned_parameter()
        server_response['fedrl'] = Config().cross_silo.rounds
        return server_response

    async def update_rl_tuned_parameter(self):
        """
        Wait for getting RL tuned parameter from env,
        and update this parameter in Config().
        """
        await self.rl_tuned_para_got.wait()
        self.rl_tuned_para_got.clear()

        Config().cross_silo = Config().cross_silo._replace(
            rounds=self.rl_tuned_para_value)

    def get_tuned_para(self, rl_tuned_para_value, time_step):
        """
        Get tuned parameter from RL env.
        This function is called by RL env.
        """
        assert time_step == self.current_round + 1
        self.rl_tuned_para_value = rl_tuned_para_value
        # Signal the RL agent that it gets the tuned parameter
        self.rl_tuned_para_got.set()
        print("RL agent: Get tuned para of time step", time_step)

    async def wrap_up_an_episode(self):
        """Wrapping up when the FL training is done."""
        if self.rl_episode >= Config().rl.episodes:
            logging.info(
                'RL Agent: Target number of training episodes reached.')
            await self.close_connections()
            sys.exit()
        else:
            # Wait until RL env resets and starts a new RL episode
            await self.new_episode_begin.wait()
            self.new_episode_begin.clear()

    @staticmethod
    def check_with_sb3_env_checker(env):
        """
        Use helper provided by stable_baselines3
        to check that the environment runs without error.
        """
        # It will check the environment and output additional warnings if needed
        check_env(env)

    @staticmethod
    def try_a_random_agent(env):
        """Quickly try a random agent on the environment."""
        # pylint: disable=unused-variable
        obs = env.reset()
        episodes = Config().rl.episodes
        n_steps = Config().training.rounds

        for i in range(episodes):
            for _ in range(n_steps):
                # Random action

                action = env.action_space.sample()
                obs, reward, done, info = env.step(action)
                if done:
                    if i < episodes:
                        obs = env.reset()
                    break