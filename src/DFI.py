from time import time

import math
import matplotlib as mpl

mpl.use('Agg')
import tensorflow as tf
from sklearn.neighbors import KNeighborsClassifier
from utils import *
from vgg19 import Vgg19
import matplotlib.pyplot as plt
import os.path


class DFI:
    """Deep Feature Interpolation procedure

    Implementation of DFI as described here:
    https://arxiv.org/pdf/1611.05507v1.pdf
    """

    def __init__(self, k=10, alpha=0.4, lamb=0.001, beta=2,
                 model_path="./model/vgg19.npy", num_layers=3,
                 gpu=True, data_dir='./data', optimizer='l-bfgs', lr=None,
                 eps=None, steps=1000, rebuild_cache=False, **kwargs):
        """
        Initialize the DFI procedure
        :param k: Number of nearest neighbours
        :param alpha: Scalar factor for w
        :param lamb: Scalar factor for total variation in loss
        :param beta: Scalar in exponent of total variation
        :param model_path: Path to the vgg19 pretrained model
        :param num_layers: Number of layers of which the deep
        features shall be extracted
        :param gpu: Compute or gpu
        :param data_dir: Directory for images
        """
        self._k = k
        self._alpha = alpha
        self._beta = beta
        self._lamb = lamb
        self._num_layers = num_layers
        self._model = load_model(model_path)
        self._gpu = gpu
        self._conv_layer_tensors = []
        self._data_dir = data_dir
        self._optimizer = optimizer
        self._eps = eps
        self._lr = lr
        self._steps = steps
        self._summaries = []
        self._rebuild_cache = rebuild_cache

        self._conv_layer_tensor_names = ['conv3_1/Relu:0',
                                         'conv4_1/Relu:0',
                                         'conv5_1/Relu:0']
        self._sess = None

    def run(self, feat='No Beard', person_index=0):
        """

        :param feat: Attribute
        :param person_index: Index of start image
        :return:
        """
        print('Starting DFI')

        # Name-scope for tensorboard
        # Setup        # Set device

        device = '/gpu:0' if self._gpu else '/cpu:0'
        phi_z = self._get_phi_z_const(device, feat, person_index)
        # Open second session with
        with tf.device(device):
            self._graph_var = tf.Graph()
            with self._graph_var.as_default():
                self._nn = Vgg19(model=self._model, input_placeholder=False, data_dir=self._data_dir)

                with tf.Session(graph=self._graph_var) as self._sess:
                    self._sess.run(tf.initialize_all_variables())

                    self._conv_layer_tensors = [
                        self._graph_var.get_tensor_by_name(
                            self._conv_layer_tensor_names[idx])
                        for idx in range(self._num_layers)]

                    # Set z_tensor reference
                    self._z_tensor = self._nn.inputRGB

                    self.optimize_z_tf(phi_z)

    def _get_phi_z_const(self, device, feat, person_index):
        if self._rebuild_cache or not os.path.isfile('cache.ch.npy'):
            print('Using device: {}'.format(device))
            with tf.device(device):
                self._graph_ph = tf.Graph()
                with self._graph_ph.as_default():
                    self._nn = Vgg19(model=self._model, input_placeholder=True)

                    with tf.name_scope('DFI-Graph') as scope:
                        # Run the graph in the session.
                        with tf.Session(graph=self._graph_ph) as self._sess:
                            self._sess.run(tf.initialize_all_variables())

                            self._conv_layer_tensors = [
                                self._graph_ph.get_tensor_by_name(
                                    self._conv_layer_tensor_names[idx])
                                for idx in range(self._num_layers)]

                            atts = load_discrete_lfw_attributes(self._data_dir)
                            imgs_path = atts['path'].values
                            start_img = \
                            reduce_img_size(load_images(*[imgs_path[0]]))[0]

                            # Get image paths
                            pos_paths, neg_paths = self._get_sets(atts, feat,
                                                                  person_index)

                            # Reduce image sizes
                            pos_imgs = reduce_img_size(load_images(*pos_paths))
                            neg_imgs = reduce_img_size(load_images(*neg_paths))

                            # Get pos/neg deep features
                            pos_deep_features = self._phi(pos_imgs)
                            neg_deep_features = self._phi(neg_imgs)

                            # Calc W
                            w = np.mean(pos_deep_features, axis=0) - np.mean(
                                neg_deep_features,
                                axis=0)
                            w /= np.linalg.norm(w)

                            # Calc phi(z)
                            phi = self._phi(start_img)
                            phi_z = phi + self._alpha * w
                            np.save('cache.ch', phi_z)
        else:
            print('Loading cached phi_z')
            phi_z = np.load('cache.ch.npy')

        return phi_z

    def optimize_z_tf(self, phi_z):
        """

        :param phi_z: phi(start_img) + alpha*w
        :param start_img: start img
        :return:
        """

        phi_z_const_tensor = tf.constant(phi_z, dtype=tf.float32,
                                         name='phi_x_alpha_w')


        # Define loss
        loss, diff_loss_tensor, tv_loss_tensor = self._minimize_z_tensor(
            phi_z_const_tensor, self._z_tensor)

        # merged = tf.summary.merge_all()

        train_writer = tf.train.SummaryWriter('log')

        if self._optimizer == 'adam':
            # Add the optimizer
            train_op = tf.train.AdamOptimizer(learning_rate=self._lr) \
                .minimize(loss, var_list=[self._z_tensor])
            # Add the ops to initialize variables.  These will include
            # the optimizer slots added by AdamOptimizer().
            init_op = tf.initialize_all_variables()

            # Actually intialize the variables
            self._sess.run(init_op)

            for i in range(self._steps + 1):
                train_op.run()

                if i % math.ceil(self._steps / 100) == 0:
                    temp_loss, diff_loss, tv_loss = \
                        self._sess.run([loss, diff_loss_tensor, tv_loss_tensor])
                    # summary = self._sess.run(merged, feed_dict=fd)
                    for sum_op in self._summaries:
                        summary = self._sess.run(sum_op)
                        train_writer.add_summary(summary, i)
                    # train_writer.add_summary(summary, i)
                    print('Step: {}'.format(i))
                    print('{:>14.10f} - loss'.format(temp_loss))
                    print('{:>14.10f} - tv_loss'.format(tv_loss))
                    print('{:>14.10f} - diff_loss'.format(diff_loss))

                    im_sum_op = tf.image_summary('img{}'.format(i), tensor=self._z_tensor, name='img'.format(i))
                    im_sum = self._sess.run(im_sum_op)

                    train_writer.add_summary(im_sum, global_step=i)

                    run = self._sess.run(self._z_tensor)
                    plt.imsave(fname='z_{}.png'.format(i),
                               arr=run[0])

    def _minimize_z_tensor(self, phi_z_const_tensor, z_tensor):
        """
        Objective function implemented with tensors
        :param phi_z_const_tensor: phi(x) + alpha*w
        :param z_tensor: Variable
        :return: loss
        """

        with tf.name_scope('summaries'):
            # Init z with the initial guess
            phi_z_prime = self._phi_tensor()
            subtract = phi_z_prime - phi_z_const_tensor
            square = tf.square(subtract)
            reduce_sum = tf.reduce_sum(square)

            regularization = self._total_variation_regularization(z_tensor,
                                                                  self._beta)
            with tf.name_scope('tv_loss'):
                tv_loss = self._lamb * tf.reduce_sum(regularization)

            with tf.name_scope('diff_loss'):
                diff_loss = 0.5 * reduce_sum

            with tf.name_scope('loss'):
                loss = diff_loss + tv_loss

            self._summaries.append(tf.scalar_summary('loss', loss))
            self._summaries.append(tf.scalar_summary('tv_loss', tv_loss))
            self._summaries.append(tf.scalar_summary('diff_loss', diff_loss))

            return loss, diff_loss, tv_loss

    def _total_variation_regularization(self, x, beta=1):
        """
        Idea from:
        https://github.com/antlerros/tensorflow-fast-neuralstyle/blob/master/net.py
        """
        wh = tf.constant([[[[1], [1], [1]]], [[[-1], [-1], [-1]]]], tf.float32)
        ww = tf.constant([[[[1], [1], [1]], [[-1], [-1], [-1]]]], tf.float32)
        tvh = lambda x: self._conv2d(x, wh, p='SAME')
        tvw = lambda x: self._conv2d(x, ww, p='SAME')
        dh = tvh(x)
        dw = tvw(x)
        tv = (tf.add(tf.reduce_sum(dh ** 2, [1, 2, 3]),
                     tf.reduce_sum(dw ** 2, [1, 2, 3]))) ** (beta / 2.)

        tv /= np.prod(x.get_shape().as_list(), dtype=np.float32) * 255

        return tv

    def _conv2d(self, x, W, strides=[1, 1, 1, 1], p='SAME', name=None):
        """2d Convolution"""
        # new_shape = tf.TensorShape([tf.Dimension(1)] + x.get_shape().dims)
        # x = tf.reshape(x, new_shape)
        return tf.nn.conv2d(x, W, strides=strides, padding=p, name=name)

    def _phi_tensor(self):
        """
        Implementation of the deep feature function in tensorflow
        :return: tensor of z in the deep feature space
        """
        # Start with first layer tensor
        res = tf.reshape(self._conv_layer_tensors[0], [-1], name='phi_z')

        # Concatenate the rest
        for i in np.arange(1, self._num_layers):
            tmp = tf.reshape(self._conv_layer_tensors[i], [-1])
            res = tf.concat(0, [res, tmp])

        self._summaries.append(tf.scalar_summary('mean0', tf.reduce_mean(self._conv_layer_tensors[0], name='mean0')))
        self._summaries.append(tf.scalar_summary('mean1', tf.reduce_mean(self._conv_layer_tensors[1], name='mean1')))
        self._summaries.append(tf.scalar_summary('mean2', tf.reduce_mean(self._conv_layer_tensors[2], name='mean2')))

        square = tf.square(res)
        reduce_sum = tf.reduce_sum(square, name='phi_tensor_sum')
        sqrt = tf.sqrt(reduce_sum, name='phi_tensor_sqrt')

        self._summaries.append(tf.scalar_summary('phi_tensor_sum', reduce_sum))
        self._summaries.append(tf.scalar_summary('phi_tensor_sqrt', sqrt))

        return res / sqrt

    def _phi(self, imgs):
        """Transform list of images into deep feature space

        :param imgs: input images
        :return: deep feature transformed images
        """

        if not isinstance(imgs, list):
            input_images = [imgs]
        else:
            input_images = imgs

        t0 = time()
        fd = {self._nn.inputRGB: input_images}
        ret = self._sess.run(self._conv_layer_tensors,
                             feed_dict=fd)
        t1 = time()
        print('Took {}'.format(t1 - t0))
        res = []

        # Create result list
        for img_idx in range(len(input_images)):
            phi_img = np.array([])

            # Append all layer results to a (M,) vector
            for layer_idx in range(self._num_layers):
                phi_img = np.append(phi_img,
                                    ret[layer_idx][img_idx].reshape(-1))

            res.append(phi_img)

        # Handle correct return type and normalize (L2)
        if not isinstance(imgs, list):
            return res[0] / np.linalg.norm(res[0])  # Single image
        else:
            return [x / np.linalg.norm(x) for x in res]  # List of images

    def _get_sets(self, atts, feat, person_index):
        """
        Generates the subsets of the knn given a person index and a feature
        :param atts: Attribute dataframe
        :param feat: Feature
        :param person_index: Person index
        :return: Positive and negative subset of the k nearest neighbours of the given person
        """
        person = atts.loc[person_index]
        del person['person']
        del person['path']

        # Remove person from df
        atts = atts.drop(person_index)

        # Split by feature
        pos_set = atts.loc[atts[feat] == 1]
        neg_set = atts.loc[atts[feat] == -1]
        pos_paths = self._get_k_neighbors(pos_set, person)
        neg_paths = self._get_k_neighbors(neg_set, person)

        return pos_paths.as_matrix(), neg_paths.as_matrix()

    def _get_k_neighbors(self, subset, person):
        """
        Gets the KNN, given the subset and the person
        :param subset: Subset in which the knn should look at
        :param person: Starting point
        :return: Image paths of the k nearest neighbours of the person in the subset
        """
        del subset['person']
        paths = subset['path']
        del subset['path']

        knn = KNeighborsClassifier(n_jobs=4)
        dummy_target = [0 for x in range(subset.shape[0])]
        knn.fit(X=subset.as_matrix(), y=dummy_target)
        knn_indices = knn.kneighbors(person.as_matrix(), n_neighbors=self._k,
                                     return_distance=False)[0]

        neighbor_paths = paths.iloc[knn_indices]

        return neighbor_paths

    def features(self):
        """
        Generate a list of possible attributes to choose from
        :return: List of attributes
        """

        atts = load_lfw_attributes(self._data_dir)
        del atts['path']
        del atts['person']
        return atts.columns.values
