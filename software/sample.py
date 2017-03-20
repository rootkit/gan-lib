import os

import tensorflow as tf

from utils.date_time_utils import get_timestamp


def sample(checkpoint_path, z_value=None):
    """
    Args:
        checkpoint_path (str): The checkpoint file path (.ckpt file)
        z_value (ndarray, default None): The value the noise variable z.
    """
    if checkpoint_path[-5:] == '.meta':
        checkpoint_path = checkpoint_path[:-5]
    with tf.Session() as sess:
        print("Importing meta graph")
        saver = tf.train.import_meta_graph(checkpoint_path + '.meta')
        saver.restore(sess, checkpoint_path)
        print("Getting samples tensor")
        images_tensor = tf.get_collection("generated")[0]

        feed_dict = {}
        if z_value is not None:
            print("Getting input noise tensor")
            z_tensor = tf.get_collection("z_var")[0]
            feed_dict[z_tensor.name] = z_value

        print("Sampling")
        images = sess.run(images_tensor, feed_dict=feed_dict)
        images = images[0, :].reshape(1, 28, 28, 1)

        samples_folder = os.path.relpath(checkpoint_path, 'ckt')
        samples_folder = os.path.dirname(samples_folder)
        name = 'samples_{}'.format(get_timestamp())
        samples_folder = os.path.join('logs', samples_folder, name)

        summary_writer = tf.summary.FileWriter(samples_folder, sess.graph)
        sum = tf.summary.image(name='samples', tensor=images)
        summary_op = tf.summary.merge([sum])
        summary_str = sess.run(summary_op)
        summary_writer.add_summary(summary_str, 0)
