# Copyright 2019 Xilinx Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Activation layer which applies emulates quantization during training."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from tensorflow_model_optimization.python.core.quantization.keras.vitis.utils import common_utils

activations = tf.keras.activations
logger = common_utils.VAILogger
register_keras_serializable = tf.keras.utils.register_keras_serializable


@register_keras_serializable(package='Vitis', name='NoQuantizeActivation')
class NoQuantizeActivation(object):
  """No quantize activation which simply returns the incoming tensor.

  This activation is required to distinguish between `keras.activations.linear`
  which does the same thing. The main difference is that NoQuantizeActivation should
  not have any quantize operation applied to it.
  """

  def __call__(self, x):
    return x

  def get_config(self):
    return {}

  def __eq__(self, other):
    if not other or not isinstance(other, NoQuantizeActivation):
      return False

    return True

  def __ne__(self, other):
    """Ensure this works on Python2."""
    return not self.__eq__(other)


@register_keras_serializable(package='Vitis', name='QuantizeAwareActivation')
class QuantizeAwareActivation(object):
  """Activation wrapper for quantization aware training.

  The goal of this class is to apply quantize operations during training such
  that the training network mimics quantization loss experienced in activations
  during inference.

  It introduces quantization loss before and after activations as required to
  mimic inference loss. The layer has built-in knowledge of how quantized
  activations are laid out during inference to emulate exact behavior.

  For example, ReLU activations are typically fused into their parent layer
  such as Conv/Dense. Hence, loss is introduced only after the activation has
  been applied. For Softmax on the other hand quantization loss is experienced
  both before and after the activation.

  Input shape:
    Arbitrary.

  Output shape:
    Same shape as input.
  """

  # TODO(pulkitb): Other activations such as elu, tanh etc., should just work
  # on inclusion. Verify in TFLite before enabling.

  # These activations should be quantized prior to the activation being applied.
  _PRE_QUANT_ACTIVATIONS = frozenset({
      'softmax', 'elu', 'selu', 'softplus', 'softsign', 'swish', 'gelu', 'tanh',
      'sigmoid', 'exponential', 'hard_sigmoid'
  })

  # These activations should be quantized after the activation has been applied.
  _POST_QUANT_ACTIVATIONS = frozenset({'linear', 'relu'})

  # Don't take any quantize operations for these activations.
  _NO_QUANT_ACTIVATIONS = frozenset({'NoQuantizeActivation'})

  _CUSTOM_ACTIVATION_ERR_MSG = (
      'Only some Keras activations under `tf.keras.activations` are supported. '
      'For other activations, use `Quantizer` directly, and update layer '
      'config using `QuantizeConfig`.')

  def __init__(self, activation, quantizer, mode, step, quantize_wrapper):
    """Constructs object, and initializes weights for quantization.

    Args:
      activation: Activation function to use.
      quantizer: `Quantizer` to be used to quantize the activation.
      step: Variable which tracks optimizer step.
      quantize_wrapper: `QuantizeWrapper` which owns this activation.
    """
    self.activation = activation
    self.quantizer = quantizer
    self._mode = mode
    self.step = step
    self.quantize_wrapper = quantize_wrapper

    if not self._is_supported_activation(self.activation):
      logger.error(self._CUSTOM_ACTIVATION_ERR_MSG)

    if self._should_pre_quantize():
      self._pre_activation_vars = quantizer.build(None, 'pre_activation',
                                                  quantize_wrapper)

    if self._should_post_quantize():
      self._post_activation_vars = quantizer.build(None, 'post_activation',
                                                   quantize_wrapper)

  @staticmethod
  def _name(activation):
    if hasattr(activation, '__name__'):
      return activation.__name__
    return activation.__class__.__name__

  def _is_supported_activation(self, activation):
    activation_name = self._name(activation)

    return activation_name in self._PRE_QUANT_ACTIVATIONS \
           or activation_name in self._POST_QUANT_ACTIVATIONS \
           or activation_name in self._NO_QUANT_ACTIVATIONS

  def _should_pre_quantize(self):
    return self._name(self.activation) in self._PRE_QUANT_ACTIVATIONS

  def _should_post_quantize(self):
    return self._name(self.activation) in self._POST_QUANT_ACTIVATIONS

  def _should_not_quantize(self):
    return self._name(self.activation) in self._NO_QUANT_ACTIVATIONS

  def get_quantize_info(self):
    if self._should_not_quantize():
      return {}

    quantize_info = {}
    if self._should_pre_quantize():
      quantize_info['type'] = 'pre_activation'
    if self._should_post_quantize():
      quantize_info['type'] = 'post_activation'

    quantize_info['info'] = self.quantizer.get_quantize_info()
    return quantize_info

  def set_quantize_info(self, new_quantize_info):
    if not self._should_not_quantize():
      self.quantizer.set_quantize_info(new_quantize_info['info'])

  @property
  def training(self):
    return self._training

  @training.setter
  def training(self, value):
    self._training = value

  @property
  def mode(self):
    return self._mode

  @mode.setter
  def mode(self, value):
    self._mode = value

  def _dict_vars(self, min_var, max_var):
    return {'min_var': min_var, 'max_var': max_var}

  def __call__(self, inputs, *args, **kwargs):

    def make_quantizer_fn(quantizer, x, training, mode, quantizer_vars):
      """Use currying to return True/False specialized fns to the cond."""

      def quantizer_fn():
        return quantizer(x, training, mode, weights=quantizer_vars)

      return quantizer_fn

    x = inputs
    if self._should_pre_quantize():
      x = common_utils.smart_cond(
          self._training,
          make_quantizer_fn(self.quantizer, x, True, self.mode,
                            self._pre_activation_vars),
          make_quantizer_fn(self.quantizer, x, False, self.mode,
                            self._pre_activation_vars))

    x = self.activation(x, *args, **kwargs)

    if self._should_post_quantize():
      x = common_utils.smart_cond(
          self._training,
          make_quantizer_fn(self.quantizer, x, True, self.mode,
                            self._post_activation_vars),
          make_quantizer_fn(self.quantizer, x, False, self.mode,
                            self._post_activation_vars))

    return x

  # `QuantizeAwareActivation` wraps the activation within a layer to perform
  # quantization. In the process, the layer's activation is replaced with
  # `QuantizeAwareActivation`.
  # However, when the layer is serialized and deserialized, we want the original
  # activation to be reconstructed. This ensures that when `QuantizeWrapper`
  # wraps the layer, it can again replace the original activation.

  @classmethod
  def from_config(cls, config):
    return activations.deserialize(config['activation'])

  def get_config(self):
    return {'activation': activations.serialize(self.activation)}
