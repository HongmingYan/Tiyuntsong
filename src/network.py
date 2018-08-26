import sabre
import math
import numpy as np
import dualgan as a3c
import tensorflow as tf
import os
import time
os.environ['CUDA_VISIBLE_DEVICES'] = '2'
# import tflearn

RAND_RANGE = 10000
EPS = 1e-6


class Zero(sabre.Abr):
    S_INFO = 7
    S_LEN = 10
    THROUGHPUT_NORM = 40 * 1024.0
    BITRATE_NORM = 8 * 1024.0
    TIME_NORM = 1000.0
   # A_DIM = len(VIDEO_BIT_RATE)
    ACTOR_LR_RATE = 1e-4
    CRITIC_LR_RATE = 1e-3
    GAN_LR_RATE = 1e-4
    A_DIM = 10
    # D_DIM = 5
    D_STEP = 0.5
    GRADIENT_BATCH_SIZE = 32
    GAN_CORE = 16

    def __init__(self, scope):
        self.quality = 0
        # self.last_quality = 0
        self.state = np.zeros((Zero.S_INFO, Zero.S_LEN))
        self.past_gan = np.zeros(Zero.GAN_CORE)
        self.quality_len = Zero.A_DIM

        gpu_options = tf.GPUOptions(allow_growth=True)
        self.sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))
        self.dual = a3c.DualNetwork(self.sess, scope)
        self.gan = a3c.GANNetwork(self.sess, state_dim=[
                                  Zero.S_INFO, Zero.S_LEN], learning_rate=Zero.GAN_LR_RATE, scope=scope)
        self.actor = a3c.ActorNetwork(self.sess,
                                      state_dim=[
                                          Zero.S_INFO, Zero.S_LEN], action_dim=self.quality_len,
                                      learning_rate=Zero.ACTOR_LR_RATE, scope=scope,
                                      dual=self.dual, gan=self.gan)
        self.critic = a3c.CriticNetwork(self.sess,
                                        state_dim=[Zero.S_INFO, Zero.S_LEN],
                                        learning_rate=Zero.CRITIC_LR_RATE, scope=scope,
                                        dual=self.dual, gan=self.gan)
        self.sess.run(tf.global_variables_initializer())
        self.history = []
        self.quality_history = []
        self.replay_buffer = []

        self.global_throughput = 0.0
        # self.s_batch = [np.zeros((Zero.S_INFO, Zero.S_LEN))]
        # action_vec = np.zeros(Zero.A_DIM)
        # self.a_batch = [action_vec]
        # self.r_batch = []
        # self.actor_gradient_batch = []
        # self.critic_gradient_batch = []

    def teach(self, buffer):
        for (s_batch, a_batch, r_batch) in buffer:
            _s = np.array(s_batch)
            _a = np.array(a_batch)
            # print(_s.shape, _a.shape)
            # for (_s, _a, _r) in zip(s_batch, a_batch, r_batch):
            #     _s = np.reshape(_s, (1, Zero.S_INFO, Zero.S_LEN))
            #     _a = np.reshape(_a, (1, Zero.A_DIM))
            #     if _r > 0:
            self.actor.teach(_s, _a)
            # self.replay_buffer.append((s, a, r))

    def clear(self):
        self.history = []
        self.quality_history = []
        self.replay_buffer = []

    def learn(self, ratio=1.0):
        actor_gradient_batch, critic_gradient_batch = [], []
        g_win = np.vstack(self._pull())
        for (s_batch, a_batch, r_batch, g_batch) in self.replay_buffer:
            s_batch = np.stack(s_batch, axis=0)
            a_batch = np.vstack(a_batch)
            r_batch = np.vstack(r_batch)
            g_batch = np.vstack(g_batch)
            actor_gradient, critic_gradient, _ = a3c.compute_gradients(s_batch, a_batch, r_batch, g_batch,
                                                                       actor=self.actor, critic=self.critic,
                                                                       lr_ratio=ratio)
            self.gan.optimize(s_batch, g_batch, g_win)
            actor_gradient_batch.append(actor_gradient)
            critic_gradient_batch.append(critic_gradient)

        for i in range(len(actor_gradient_batch)):
            self.actor.apply_gradients(actor_gradient_batch[i], lr_ratio=ratio)
            self.critic.apply_gradients(
                critic_gradient_batch[i], lr_ratio=ratio)

        self.actor_gradient_batch = []
        self.critic_gradient_batch = []

    def _pull(self):
        _g = []
        for (_, _, r_batch, g_batch) in self.replay_buffer:
            for (r, g) in zip(r_batch, g_batch):
                if r > 0:
                    _g.append(g)
        return _g

    def get_action(self):
        return self.history

    def push(self, reward):
        s_batch, a_batch, r_batch, g_batch = [], [], [], []
        assert len(self.history) == len(reward)
        _index = 0
        for (state, action, gan) in self.history:
            s_batch.append(state)
            a_batch.append(action)
            r_batch.append(reward[_index])
            g_batch.append(gan)
            _index += 1

        self.replay_buffer.append((s_batch, a_batch, r_batch, g_batch))

        self.history = []
        self.quality_history = []
        self.state = np.zeros((Zero.S_INFO, Zero.S_LEN))
        self.past_gan = np.zeros(Zero.GAN_CORE)

    def _get_quality_delay(self, action):
        return action // Zero.A_DIM, action % Zero.A_DIM

    def get_quality_delay(self, segment_index):
        if segment_index != 1:
            self.quality_history.append(
                (sabre.played_bitrate, sabre.rebuffer_time, sabre.total_bitrate_change))
        if segment_index < 0:
            return

        download_time, throughput, _, _, _ = sabre.log_history[-1]
        state = self.state
        state = np.roll(state, -1, axis=1)

        state[0, -1] = min(throughput / Zero.THROUGHPUT_NORM, 1.0)
        state[1, -1] = min(download_time / (10 * Zero.TIME_NORM), 1.0)
        state[2, -1] = self.quality / self.quality_len
        state[3, -1] = (len(sabre.manifest.segments) -
                        segment_index) / len(sabre.manifest.segments)
        state[4, -1] = sabre.get_buffer_level() / (25 * Zero.TIME_NORM)

        for p in range(Zero.S_INFO - 2):
            if state[p, -1] > 1.0:
                self.global_throughput = max(
                    self.global_throughput, throughput)
                print('overflow', p, state[p, -1], self.global_throughput)

        state[5, 0:Zero.A_DIM] = np.array(sabre.manifest.bitrates /
                                          np.max(sabre.manifest.bitrates))
        state[6, 0:Zero.A_DIM] = np.array(
            sabre.manifest.segments[segment_index]) / 1024.0 / 1024.0 / 2.0

        self.state = state
        past_gan, action_prob = self.actor.predict(
            np.reshape(self.state, (1, Zero.S_INFO, Zero.S_LEN)),
            np.reshape(self.past_gan, (1, Zero.GAN_CORE)))
        action_cumsum = np.cumsum(action_prob[0])
        quality = (action_cumsum > np.random.randint(
            1, RAND_RANGE) / float(RAND_RANGE)).argmax()

        action_vec = np.zeros(Zero.A_DIM)
        action_vec[quality] = 1

        _delay = 0.0
        self.history.append((self.state, action_vec, self.past_gan))
        self.past_gan = past_gan
        self.quality = quality
        return (quality, _delay)
