import cPickle as pkl
import os
import sys

import numpy as np
import prettytensor as pt
import tensorflow as tf
from progressbar import ETA, Bar, Percentage, ProgressBar

from infogan.models.regularized_gan import RegularizedGAN

TINY = 1e-8


def apply_optimizer(optimizer, losses, var_list, clip_by_value=None,
                    name='cost'):
    total_loss = tf.add_n(losses, name=name)
    grads_and_vars = optimizer.compute_gradients(total_loss, var_list=var_list)
    if clip_by_value is not None:
        clipped_grads_and_vars = []
        for g, v in grads_and_vars:
            cg = None
            if g is not None:
                cg = tf.clip_by_value(g, *clip_by_value)
            clipped_grads_and_vars.append((cg, v))
        grads_and_vars = clipped_grads_and_vars
    train_op = optimizer.apply_gradients(grads_and_vars)
    return train_op


class Trainer(object):
    def __init__(self,
                 model,
                 batch_size,
                 discrim_optimizer,
                 generator_optimizer,
                 dataset=None,
                 exp_name="experiment",
                 log_dir="logs",
                 checkpoint_dir="ckt",
                 max_epoch=100,
                 updates_per_epoch=100,
                 snapshot_interval=500,
                 info_reg_coeff=1.0,
                 gen_disc_update_ratio=1,
                 generator_grad_clip_by_value=None,
                 discrim_grad_clip_by_value=None,
                 ):
        """
        :type model: RegularizedGAN
        """
        self.model = model
        self.dataset = dataset
        self.batch_size = batch_size
        self.generator_optimizer = generator_optimizer
        self.discrim_optimizer = discrim_optimizer
        self.max_epoch = max_epoch
        self.exp_name = exp_name
        self.log_dir = log_dir
        self.checkpoint_dir = checkpoint_dir
        self.snapshot_interval = snapshot_interval
        self.updates_per_epoch = updates_per_epoch
        self.info_reg_coeff = info_reg_coeff
        self.discriminator_trainer = None
        self.generator_trainer = None
        self.g_input = None
        self.d_input = None
        self.log_vars = []
        self.gen_disc_update_ratio = gen_disc_update_ratio
        self.discrim_grad_clip_by_value = discrim_grad_clip_by_value
        self.generator_grad_clip_by_value = generator_grad_clip_by_value
        self.discrim_loss = None
        self.generator_loss = None
        self.fake_reg_z_dist_info = None
        self.fake_x = None

    def get_discriminator_loss(self, real_d, fake_d):
        raise NotImplementedError

    def get_generator_loss(self, fake_d):
        raise NotImplementedError

    def prepare_g_input(self):
        raise NotImplementedError

    def prepare_d_input(self):
        d_input_shape = [self.batch_size, self.d_input_dim]
        self.d_input = tf.placeholder(tf.float32, d_input_shape)

    def init_opt(self):
        self.prepare_g_input()
        self.prepare_d_input()

        with pt.defaults_scope(phase=pt.Phase.train):
            # (n, d)
            fake_x, _, x_dist_flat = self.model.generate(self.g_input)
            # (n,)
            real_d, _, _, _ = self.model.discriminate(self.d_input)
            fake_d, _, fake_reg_z_dist_info, _ = self.model.discriminate(fake_x)

            self.log_vars.append(("max_real_d", tf.reduce_max(real_d)))
            self.log_vars.append(("min_real_d", tf.reduce_min(real_d)))
            self.log_vars.append(("max_fake_d", tf.reduce_max(fake_d)))
            self.log_vars.append(("min_fake_d", tf.reduce_min(fake_d)))
            self.fake_reg_z_dist_info = fake_reg_z_dist_info
            self.fake_x = fake_x

            self.discrim_loss = self.get_discriminator_loss(real_d, fake_d)
            self.generator_loss = self.get_generator_loss(fake_d)

            self.log_vars.append(("discriminator_loss", self.discrim_loss))
            self.log_vars.append(("generator_loss", self.generator_loss))

            for k, v in self.log_vars:
                tf.summary.scalar(name=k, tensor=v)

        with pt.defaults_scope(phase=pt.Phase.test):
            with tf.variable_scope("train_samples", reuse=True):
                self.model.get_train_samples()

        tf.add_to_collection("z_var", self.g_input)
        tf.add_to_collection("x_dist_flat", x_dist_flat)

    def init_optimizers(self):
        with pt.defaults_scope(phase=pt.Phase.train):
            all_vars = tf.trainable_variables()
            self.d_vars = [var for var in all_vars if var.name.startswith('d_')]
            self.g_vars = [var for var in all_vars if var.name.startswith('g_')]


            self.discriminator_trainer = apply_optimizer(self.discrim_optimizer,
                                                         losses=[self.discrim_lossdiscrim_loss],
                                                         var_list=self.d_vars,
                                                         clip_by_value=self.discrim_grad_clip_by_value)

            self.generator_trainer = apply_optimizer(self.generator_optimizer,
                                                     losses=[self.generator_loss],
                                                     var_list=self.g_vars,
                                                     clip_by_value=self.generator_grad_clip_by_value)

    def update(self, sess, i, log_vars, all_log_vals):
        raise NotImplementedError

    def train(self):
        with open(os.path.join(self.checkpoint_dir, 'model.pkl'), 'wb') as f:
            pkl.dump(self.model, f)

        self.init_opt()
        init = tf.global_variables_initializer()

        with tf.Session() as sess:
            sess.run(init)

            summary_op = tf.summary.merge_all()
            summary_writer = tf.summary.FileWriter(self.log_dir, sess.graph)

            saver = tf.train.Saver()

            counter = 0

            log_vars = [x for _, x in self.log_vars]
            log_keys = [x for x, _ in self.log_vars]

            for epoch in range(self.max_epoch):
                widgets = ["epoch #%d|" % epoch, Percentage(), Bar(), ETA()]
                pbar = ProgressBar(maxval=self.updates_per_epoch, widgets=widgets)
                pbar.start()

                all_log_vals = []
                for i in range(self.updates_per_epoch):
                    pbar.update(i)
                    all_log_vals = self.update(sess, i, log_vars, all_log_vals)
                    counter += 1

                    if counter % self.snapshot_interval == 0:
                        snapshot_name = "%s_%s" % (self.exp_name, str(counter))
                        fn = saver.save(sess, "%s/%s.ckpt" % (self.checkpoint_dir, snapshot_name))
                        print("Model saved in file: %s" % fn)

                # (n, h, w, c)
                x, _ = self.dataset.train.next_batch(self.batch_size)

                summary_str = sess.run(summary_op, {self.d_input: x})
                summary_writer.add_summary(summary_str, counter)

                avg_log_vals = np.mean(np.array(all_log_vals), axis=0)
                log_dict = dict(zip(log_keys, avg_log_vals))

                log_line = "; ".join("%s: %s" % (str(k), str(v)) for k, v in zip(log_keys, avg_log_vals))
                print("Epoch %d | " % (epoch) + log_line)
                sys.stdout.flush()
                if np.any(np.isnan(avg_log_vals)):
                    raise ValueError("NaN detected!")


class GANTrainer(Trainer):
    def prepare_g_input(self):
        self.g_input = self.model.latent_dist.sample_prior(self.batch_size)


class InfoGANTrainer(GANTrainer):
    def init_opt(self):
        super(GANTrainer).init_opt()
        with pt.defaults_scope(phase=pt.Phase.train):
            mi_est = tf.constant(0.)
            cross_ent = tf.constant(0.)
            reg_z = self.model.reg_z(self.g_input)
            # compute for discrete and continuous codes separately
            # discrete:
            if len(self.model.reg_disc_latent_dist.dists) > 0:
                disc_reg_z = self.model.disc_reg_z(reg_z)
                disc_reg_dist_info = self.model.disc_reg_dist_info(self.fake_reg_z_dist_info)
                disc_log_q_c_given_x = self.model.reg_disc_latent_dist.logli(disc_reg_z, disc_reg_dist_info)
                disc_log_q_c = self.model.reg_disc_latent_dist.logli_prior(disc_reg_z)
                disc_cross_ent = tf.reduce_mean(-disc_log_q_c_given_x)
                disc_ent = tf.reduce_mean(-disc_log_q_c)
                disc_mi_est = disc_ent - disc_cross_ent
                mi_est += disc_mi_est
                cross_ent += disc_cross_ent
                self.log_vars.append(("MI_disc", disc_mi_est))
                self.log_vars.append(("CrossEnt_disc", disc_cross_ent))
                self.discrim_loss -= self.info_reg_coeff * disc_mi_est
                self.generator_loss -= self.info_reg_coeff * disc_mi_est

            if len(self.model.reg_cont_latent_dist.dists) > 0:
                cont_reg_z = self.model.cont_reg_z(reg_z)
                cont_reg_dist_info = self.model.cont_reg_dist_info(self.fake_reg_z_dist_info)
                cont_log_q_c_given_x = self.model.reg_cont_latent_dist.logli(cont_reg_z, cont_reg_dist_info)
                cont_log_q_c = self.model.reg_cont_latent_dist.logli_prior(cont_reg_z)
                cont_cross_ent = tf.reduce_mean(-cont_log_q_c_given_x)
                cont_ent = tf.reduce_mean(-cont_log_q_c)
                cont_mi_est = cont_ent - cont_cross_ent
                mi_est += cont_mi_est
                cross_ent += cont_cross_ent
                self.log_vars.append(("MI_cont", cont_mi_est))
                self.log_vars.append(("CrossEnt_cont", cont_cross_ent))
                self.discrim_loss -= self.info_reg_coeff * cont_mi_est
                self.generator_loss -= self.info_reg_coeff * cont_mi_est

            for idx, dist_info in enumerate(self.model.reg_latent_dist.split_dist_info(self.fake_reg_z_dist_info)):
                if "stddev" in dist_info:
                    self.log_vars.append(("max_std_%d" % idx, tf.reduce_max(dist_info["stddev"])))
                    self.log_vars.append(("min_std_%d" % idx, tf.reduce_min(dist_info["stddev"])))

            self.log_vars.append(("MI", mi_est))
            self.log_vars.append(("CrossEnt", cross_ent))
































    def __init__(self,
                 discrim_learning_rate=2e-4,
                 generator_learning_rate=1e-3,
                 **kwargs):
        d_optim = tf.train.AdamOptimizer(discrim_learning_rate, beta1=0.5)
        kwargs.setdefault('discrim_optimizer', d_optim)
        g_optim = tf.train.AdamOptimizer(generator_learning_rate, beta1=0.5)
        kwargs.setdefault('generator_optimizer', g_optim)
        super(InfoGANTrainer, self).__init__(**kwargs)

    def get_discriminator_loss(self, real_d, fake_d):
        real = tf.log(real_d + TINY)
        fake = tf.log(1. - fake_d + TINY)
        return - tf.reduce_mean(real + fake)

    def get_generator_loss(self, fake_d):
        return - tf.reduce_mean(tf.log(fake_d + TINY))

    def update(self, sess, i, log_vars, all_log_vals):
        x, _ = self.dataset.train.next_batch(self.batch_size)
        feed_dict = {self.input_tensor: x}
        sess.run(self.generator_trainer, feed_dict)
        if i % self.gen_disc_update_ratio == 0:
            log_vals = sess.run([self.discriminator_trainer] + log_vars, feed_dict)[1:]
            all_log_vals.append(log_vals)
        return all_log_vals


class WassersteinGANTrainer(GANTrainer):
    def __init__(self,
                 discrim_learning_rate=5e-5,
                 generator_learning_rate=5e-5,
                 **kwargs):
        self.n_critic = 5
        self.discrim_weight_clip_by_value = [-0.01, 0.01]
        self.clip = None
        d_optim = tf.train.RMSPropOptimizer(discrim_learning_rate)
        kwargs.setdefault('discrim_optimizer', d_optim)
        g_optim = tf.train.RMSPropOptimizer(generator_learning_rate)
        kwargs.setdefault('generator_optimizer', g_optim)
        super(WassersteinGANTrainer, self).__init__(**kwargs)

    def get_discriminator_loss(self, real_d, fake_d):
        return tf.reduce_mean(real_d - fake_d)

    def get_generator_loss(self, fake_d):
        return tf.reduce_mean(fake_d)

    def update(self, sess, i, log_vars, all_log_vals):
        for _ in range(self.n_critic):
            x, _ = self.dataset.train.next_batch(self.batch_size)
            feed_dict = {self.input_tensor: x}
            log_vals = sess.run([self.discriminator_trainer] + log_vars,
                                feed_dict)[1:]
            if self.clip is None:
                self.clip = [tf.assign(
                    d, tf.clip_by_value(d, *self.discrim_weight_clip_by_value))
                             for d in self.d_vars]
            sess.run(self.clip)
            all_log_vals.append(log_vals)
        x, _ = self.dataset.train.next_batch(self.batch_size)
        feed_dict = {self.input_tensor: x}
        sess.run(self.generator_trainer, feed_dict)
        return all_log_vals
