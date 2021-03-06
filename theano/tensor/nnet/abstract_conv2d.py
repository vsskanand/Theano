"""
Define abstract conv2d interface
"""
import logging

import theano
from theano.tensor import (as_tensor_variable, patternbroadcast)
from theano.tensor import TensorType
from theano.gof import Apply, Op
from theano.gof import local_optimizer
from theano.tensor.opt import register_specialize_device

# Cpu implementation
from theano.tensor.nnet import conv2d as cpu_conv2d, ConvOp
from theano.tensor.nnet.ConvGrad3D import convGrad3D
from theano.tensor.nnet.ConvTransp3D import convTransp3D

__docformat__ = "restructuredtext en"

_logger = logging.getLogger("theano.tensor.nnet.conv2d")


def conv2d(input,
           filters,
           input_shape=None,
           filter_shape=None,
           border_mode='valid',
           subsample=(1, 1),
           filter_flip=True):
    """
    This function will build the symbolic graph for convolving a mini-batch of a
    stack of 2D inputs with a set of 2D filters. The implementation is modelled
    after Convolutional Neural Networks (CNN).

    :type input: symbolic 4D tensor
    :param input: mini-batch of feature map stacks, of shape
        (batch size, input channels, input rows, input columns).
        See the optional parameter ``input_shape``.

    :type filters: symbolic 4D tensor
    :param filters: set of filters used in CNN layer of shape
        (output channels, input channels, filter rows, filter columns).
        See the optional parameter ``filter_shape``.

    :type input_shape: None, tuple/list of len 4 of int or Constant variable
    :param input_shape: The shape of the input parameter.
        Optional, possibly used to choose an optimal implementation.
        You can give ``None`` for any element of the list to specify that this
        element is not known at compile time.

    :type filter_shape: None, tuple/list of len 4 of int or Constant variable
    :param filter_shape: The shape of the filters parameter.
        Optional, possibly used to choose an optimal implementation.
        You can give ``None`` for any element of the list to specify that this
        element is not known at compile time.

    :type border_mode: str, int or tuple of two int
    :param border_mode: Either of the following:
        * ``'valid'``: apply filter wherever it completely overlaps with the
          input. Generates output of shape: input shape - filter shape + 1
        * ``'full'``: apply filter wherever it partly overlaps with the input.
          Generates output of shape: input shape + filter shape - 1
        * ``'half'``: pad input with a symmetric border of ``filter rows // 2``
          rows and ``filter columns // 2`` columns, then perform a valid
          convolution. For filters with an odd number of rows and columns, this
          leads to the output shape being equal to the input shape.
        * ``int``: pad input with a symmetric border of zeros of the given
          width, then perform a valid convolution.
        * ``(int1, int2)``: pad input with a symmetric border of ``int1`` rows
          and ``int2`` columns, then perform a valid convolution.

    :type subsample: tuple of len 2
    :param subsample: factor by which to subsample the output.
        Also called strides elsewhere.

    :type filter_flip: bool
    :param filter_flip: If ``True``, will flip the filter rows and columns
        before sliding them over the input. This operation is normally referred
        to as a convolution, and this is the default. If ``False``, the filters
        are not flipped and the operation is referred to as a cross-correlation.

    :rtype: symbolic 4D tensor
    :return: set of feature maps generated by convolutional layer. Tensor is
        of shape (batch size, output channels, output rows, output columns)
    """

    conv_op = AbstractConv2d(imshp=input_shape,
                             kshp=filter_shape,
                             border_mode=border_mode,
                             subsample=subsample,
                             filter_flip=filter_flip)
    return conv_op(input, filters)


class BaseAbstractConv2d(Op):
    """
    Base class for AbstractConv
    Define an abstract convolution op that will be replaced with the appropriate implementation

    :type imshp: None, tuple/list of len 4 of int or Constant variable
    :param imshp: The shape of the input parameter.
        Optional, possibly used to choose an optimal implementation.
        You can give ``None`` for any element of the list to specify that this
        element is not known at compile time.
        imshp is defined w.r.t the forward conv.

    :type kshp: None, tuple/list of len 4 of int or Constant variable
    :param kshp: The shape of the filters parameter.
        Optional, possibly used to choose an optimal implementation.
        You can give ``None`` for any element of the list to specify that this
        element is not known at compile time.
        kshp is defined w.r.t the forward conv.

    :type border_mode: str, int or tuple of two int
    :param border_mode: Either of the following:
        * ``'valid'``: apply filter wherever it completely overlaps with the
          input. Generates output of shape: input shape - filter shape + 1
        * ``'full'``: apply filter wherever it partly overlaps with the input.
          Generates output of shape: input shape + filter shape - 1
        * ``'half'``: pad input with a symmetric border of ``filter rows // 2``
          rows and ``filter columns // 2`` columns, then perform a valid
          convolution. For filters with an odd number of rows and columns, this
          leads to the output shape being equal to the input shape.
        * ``int``: pad input with a symmetric border of zeros of the given
          width, then perform a valid convolution.
        * ``(int1, int2)``: pad input with a symmetric border of ``int1`` rows
          and ``int2`` columns, then perform a valid convolution.

    :type subsample: tuple of len 2
    :param subsample: factor by which to subsample the output.
        Also called strides elsewhere.

    :type filter_flip: bool
    :param filter_flip: If ``True``, will flip the filter rows and columns
        before sliding them over the input. This operation is normally referred
        to as a convolution, and this is the default. If ``False``, the filters
        are not flipped and the operation is referred to as a cross-correlation.
    """
    check_broadcast = False
    __props__ = ('border_mode', 'subsample', 'filter_flip', 'imshp', 'kshp')

    def __init__(self,
                 imshp=None, kshp=None,
                 border_mode="valid", subsample=(1, 1),
                 filter_flip=True):
        if isinstance(border_mode, int):
            border_mode = (border_mode, border_mode)
        if isinstance(border_mode, tuple):
            pad_h, pad_w = map(int, border_mode)
            border_mode = (pad_h, pad_w)
        if not ((isinstance(border_mode, tuple) and min(border_mode) >= 0) or
                border_mode in ('valid', 'full', 'half')):
            raise ValueError(
                'invalid border_mode {}, which must be either '
                '"valid", "full", "half", an integer or a pair of'
                ' integers'.format(border_mode))

        self.imshp = imshp
        self.kshp = kshp
        self.border_mode = border_mode
        self.filter_flip = filter_flip

        if len(subsample) != 2:
            raise ValueError("subsample must have two elements")
        self.subsample = subsample

    def flops(self, inp, outp):
        """ Useful with the hack in profilemode to print the MFlops"""
        # if the output shape is correct, then this gives the correct
        # flops for any direction, sampling, padding, and border mode
        inputs, filters = inp
        outputs, = outp
        assert inputs[1] == filters[1]
        # nb mul and add by output pixel
        flops = filters[2] * filters[3] * 2
        # nb flops by output image
        flops *= outputs[2] * outputs[3]
        # nb patch multiplied
        flops *= inputs[1] * filters[0] * inputs[0]
        return flops


class AbstractConv2d(BaseAbstractConv2d):
    """
    Abstract Op for the forward convolution.
    """

    def __init__(self,
                 imshp=None,
                 kshp=None,
                 border_mode="valid",
                 subsample=(1, 1),
                 filter_flip=True):
        super(AbstractConv2d, self).__init__(imshp, kshp,
                                             border_mode, subsample, filter_flip)

    def make_node(self, img, kern):
        if img.type.ndim != 4:
            raise TypeError('img must be 4D tensor')
        if kern.type.ndim != 4:
            raise TypeError('kern must be 4D tensor')

        broadcastable = [img.broadcastable[0],
                         kern.broadcastable[0],
                         False, False]
        output = img.type.clone(broadcastable=broadcastable)()
        return Apply(self, [img, kern], [output])

    def perform(self, node, inp, out_):
        raise NotImplementedError('AbstractConv2d theano optimization failed')

    def grad(self, inp, grads):
        bottom, weights = inp
        top, = grads
        d_bottom = AbstractConv2d_gradInputs(self.imshp, self.kshp,
                                             self.border_mode,
                                             self.subsample,
                                             self.filter_flip)(
            weights, top, bottom.shape[-2:])
        d_weights = AbstractConv2d_gradWeights(self.imshp, self.kshp,
                                               self.border_mode,
                                               self.subsample,
                                               self.filter_flip)(
            bottom, top, weights.shape[-2:])
        return d_bottom, d_weights


class AbstractConv2d_gradWeights(BaseAbstractConv2d):
    """Gradient wrt. filters for `AbstractConv2d`.

    :note: You will not want to use this directly, but rely on
           Theano's automatic differentiation or graph optimization to
           use it as needed.

    """
    def __init__(self,
                 imshp=None,
                 kshp=None,
                 border_mode="valid",
                 subsample=(1, 1),
                 filter_flip=True):
        super(AbstractConv2d_gradWeights, self).__init__(imshp, kshp,
                                                         border_mode, subsample, filter_flip)

    # Update shape/height_width
    def make_node(self, img, topgrad, shape):
        if img.type.ndim != 4:
            raise TypeError('img must be 4D tensor')
        if topgrad.type.ndim != 4:
            raise TypeError('topgrad must be 4D tensor')

        shape = as_tensor_variable(shape)
        broadcastable = [topgrad.broadcastable[1],
                         img.broadcastable[1],
                         False, False]
        output = img.type.clone(broadcastable=broadcastable)()
        return Apply(self, [img, topgrad, shape], [output])

    def perform(self, node, inp, out_):
        raise NotImplementedError('AbstractConv2d_gradWeight theano optimization failed')

    def grad(self, inp, grads):
        bottom, top = inp[:2]
        weights, = grads
        d_bottom = AbstractConv2d_gradInputs(self.imshp, self.kshp,
                                             self.border_mode,
                                             self.subsample,
                                             self.filter_flip)(weights, top, bottom.shape[-2:])
        d_top = AbstractConv2d(self.imshp,
                               self.kshp,
                               self.border_mode,
                               self.subsample,
                               self.filter_flip)(bottom, weights)
        d_height_width = (theano.gradient.DisconnectedType()(),)
        return (d_bottom, d_top) + d_height_width

    def connection_pattern(self, node):
        return [[1], [1], [0]]  # no connection to height, width


class AbstractConv2d_gradInputs(BaseAbstractConv2d):
    """Gradient wrt. inputs for `AbstractConv2d`.

    :note: You will not want to use this directly, but rely on
           Theano's automatic differentiation or graph optimization to
           use it as needed.

    """

    def __init__(self,
                 imshp=None,
                 kshp=None,
                 border_mode="valid",
                 subsample=(1, 1),
                 filter_flip=True):
        super(AbstractConv2d_gradInputs, self).__init__(imshp, kshp,
                                                        border_mode, subsample, filter_flip)

    # Update shape/height_width
    def make_node(self, kern, topgrad, shape):
        if kern.type.ndim != 4:
            raise TypeError('kern must be 4D tensor')
        if topgrad.type.ndim != 4:
            raise TypeError('topgrad must be 4D tensor')

        shape = as_tensor_variable(shape)
        broadcastable = [topgrad.type.broadcastable[0],
                         kern.type.broadcastable[1],
                         False, False]
        output = kern.type.clone(broadcastable=broadcastable)()
        return Apply(self, [kern, topgrad, shape], [output])

    def perform(self, node, inp, out_):
        raise NotImplementedError('AbstractConv2d_gradWeight theano optimization failed')

    def grad(self, inp, grads):
        weights, top = inp[:2]
        bottom, = grads
        d_weights = AbstractConv2d_gradWeights(self.imshp, self.kshp,
                                               self.border_mode,
                                               self.subsample)(bottom, top, weights.shape[-2:])
        d_top = AbstractConv2d(self.imshp, self.kshp,
                               self.border_mode, self.subsample)(bottom, weights)
        d_height_width = (theano.gradient.DisconnectedType()(),)
        return (d_weights, d_top) + d_height_width

    def connection_pattern(self, node):
        return [[1], [1], [0]]  # no connection to height, width


# Cpu Optmization
@local_optimizer([AbstractConv2d])
def local_conv2d_cpu(node):

    if not isinstance(node.op, AbstractConv2d):
        return None

    img, kern = node.inputs
    if ((not isinstance(img.type, TensorType) or
         not isinstance(kern.type, TensorType))):
        return None
    if node.op.border_mode not in ['full', 'valid']:
        return None
    if not node.op.filter_flip:
        # Not tested yet
        return None

    rval = cpu_conv2d(img, kern,
                      node.op.imshp, node.op.kshp,
                      border_mode=node.op.border_mode,
                      subsample=node.op.subsample)
    return [rval]
register_specialize_device(local_conv2d_cpu, 'fast_compile')


@local_optimizer([AbstractConv2d_gradWeights])
def local_conv2d_gradweight_cpu(node):

    img, topgrad, shape = node.inputs

    if ((not isinstance(img.type, TensorType) or
         not isinstance(topgrad.type, TensorType))):
        return None
    if node.op.border_mode not in ['full', 'valid']:
        return None
    if not node.op.filter_flip:
        # Not tested yet
        return

    if node.op.border_mode == 'valid' and \
            (node.op.subsample != (1, 1)):
        # Use the gradient as defined in conv3D, because the implementation
        # by Conv is slow (about 3x slower than conv3D, and probably 10x
        # slower than it could be), nad incorrect when subsample > 2.
        # build a "node", that should be equivalent to the one given by
        # self.make_node, but using convGrad3D instead.
        shuffled_img = img.dimshuffle(0, 2, 3, 'x', 1)
        shuffled_topgrad = topgrad.dimshuffle(0, 2, 3, 'x', 1)
        rval = convGrad3D(V=shuffled_img,
                          d=(node.op.subsample[0], node.op.subsample[1], 1),
                          WShape=(shuffled_topgrad.shape[4],
                                  shape[0], shape[1], 1,
                                  shuffled_img.shape[4]),
                          dCdH=shuffled_topgrad)

        rval = theano.tensor.addbroadcast(rval, 3)
        rval = rval.dimshuffle(0, 4, 1, 2)
        rval = rval[:, :, ::-1, ::-1]
        rval = patternbroadcast(rval, node.outputs[0].broadcastable)
        return [rval]

    dx, dy = node.op.subsample
    if dx not in (1, 2) or dy not in (1, 2):
        # Not implemented in the gradient of ConvOp
        return None

    if node.op.imshp is None:
        op_imshp = (None, None, None, None)
    else:
        op_imshp = node.op.imshp

    if node.op.kshp is None:
        op_kshp = (None, None, None, None)
    else:
        op_kshp = node.op.kshp

    if None in op_imshp or None in op_kshp:
        if (dx, dy) != (1, 1):
            # We cannot infer the shapes
            return None

    # Determine gradient on kernels
    assert len(op_imshp) == 4 and len(op_kshp) == 4

    outshp = ConvOp.getOutputShape(op_imshp[2:],
                                   op_kshp[2:], node.op.subsample,
                                   node.op.border_mode)
    fulloutshp = ConvOp.getOutputShape(op_imshp[2:],
                                       op_kshp[2:], (1, 1),
                                       node.op.border_mode)

    newimg = img.dimshuffle((1, 0, 2, 3))
    newtopgrad = topgrad.dimshuffle((1, 0, 2, 3))

    if node.op.border_mode == 'valid':
        (img, filters) = (newimg, newtopgrad)
        kshp_logical = fulloutshp
        kshp_logical_top_aligned = False
        imshp_logical = None
        (bsize, nkern) = (op_imshp[1], op_kshp[0])
        imshp = (op_imshp[0], op_imshp[2], op_imshp[3])
        kshp = outshp
    elif node.op.border_mode == 'full':
        (img, filters) = (newtopgrad, newimg)
        kshp_logical = None
        kshp_logical_top_aligned = True
        imshp_logical = (op_imshp[0],
                         fulloutshp[0],
                         fulloutshp[1])
        (bsize, nkern) = (op_kshp[0], op_imshp[1])
        imshp = (op_imshp[0], outshp[0], outshp[1])
        kshp = op_imshp[2:]
    else:
        raise NotImplementedError(
            'Only [full,valid] modes are currently supported.')

    # Flip the kernels
    filters = filters[:, :, ::-1, ::-1]

    dw = ConvOp(imshp, kshp, nkern, bsize, 1, 1, output_mode='valid',
                unroll_batch=None, unroll_kern=None, unroll_patch=None,
                imshp_logical=imshp_logical,
                kshp_logical=kshp_logical,
                kshp_logical_top_aligned=kshp_logical_top_aligned,
                direction_hint='bprop weights')
    res = dw(img, filters)
    if node.op.border_mode == 'valid':
        res = res.dimshuffle((1, 0, 2, 3))
        res = res[:, :, ::-1, ::-1]

    res = patternbroadcast(res, node.outputs[0].broadcastable)
    return [res]
register_specialize_device(local_conv2d_gradweight_cpu, 'fast_compile')


@local_optimizer([AbstractConv2d_gradInputs])
def local_conv2d_gradinputs_cpu(node):
    kern, topgrad, shape = node.inputs

    if ((not isinstance(kern.type, TensorType) or
         not isinstance(topgrad.type, TensorType))):
        return None
    if node.op.border_mode not in ['full', 'valid']:
        return None
    if not node.op.filter_flip:
        # Not tested yet
        return None

    # Conv 3d implementation, needed when subsample > 2
    if node.op.border_mode == 'valid' and node.op.subsample != (1, 1):
        kern = kern[:, :, ::-1, ::-1]
        shuffled_kern = kern.dimshuffle(0, 2, 3, 'x', 1)
        shuffled_topgrad = topgrad.dimshuffle(0, 2, 3, 'x', 1)
        b = theano.tensor.zeros_like(shuffled_kern[0, 0, 0, 0, :])
        rval = convTransp3D(W=shuffled_kern, b=b,
                            d=(node.op.subsample[0], node.op.subsample[1], 1),
                            H=shuffled_topgrad,
                            RShape=(shape[0], shape[1], 1))
        rval = theano.tensor.addbroadcast(rval, 3)
        rval = rval.dimshuffle(0, 4, 1, 2)
        rval = patternbroadcast(rval, node.outputs[0].broadcastable)
        return [rval]

    # Conv2d Implementation
    dx, dy = node.op.subsample
    if dx not in (1, 2) or dy not in (1, 2):
        # Not implemented in the gradient of ConvOp
        return None

    if node.op.imshp is None:
        op_imshp = (None, None, None, None)
    else:
        op_imshp = node.op.imshp

    if node.op.kshp is None:
        op_kshp = (None, None, None, None)
    else:
        op_kshp = node.op.kshp

    if None in op_imshp or None in op_kshp:
        if (dx, dy) != (1, 1):
            return None

    mode = 'valid'
    if not node.op.border_mode == 'full':
        mode = 'full'
    filters = kern.dimshuffle((1, 0, 2, 3))
    filters = filters[:, :, ::-1, ::-1]

    outshp = ConvOp.getOutputShape(op_imshp[2:],
                                   op_kshp[2:], node.op.subsample,
                                   node.op.border_mode)
    fulloutshp = ConvOp.getOutputShape(op_imshp[2:],
                                       op_kshp[2:], (1, 1),
                                       node.op.border_mode)
    nkern = op_imshp[1]
    imshp = (op_kshp[0], outshp[0], outshp[1])
    imshp_logical = (op_kshp[0], fulloutshp[0], fulloutshp[1])
    din = ConvOp(imshp,
                 op_kshp[2:],
                 nkern,
                 op_imshp[0],
                 1, 1, output_mode=mode,
                 unroll_batch=None, unroll_kern=None,
                 unroll_patch=None,
                 imshp_logical=imshp_logical,
                 kshp_logical=None,
                 version=-1,
                 direction_hint='bprop inputs')
    din = din(topgrad, filters)
    din = patternbroadcast(din, node.outputs[0].broadcastable)
    return [din]
register_specialize_device(local_conv2d_gradinputs_cpu, 'fast_compile')
