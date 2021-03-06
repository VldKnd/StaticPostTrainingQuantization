import torch
from torch import nn
from torch.nn.quantized import functional as qF
from torch.nn.quantized import DeQuantize

class Q(nn.Module):

    def __init__(self, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Basic block for quantization for functions from torch.nn.*.
        This is a common for all the blocks class, which is used to init all other types of quantizatino.

        Parameters
        ----------
        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__()
        self.weight_dtype = weight_dtype
        self.activation_dtype =activation_dtype
        self._computed = False
        self.d_min = None
        self.d_max = None
        self.q_min = torch.iinfo(activation_dtype).min
        self.q_max = torch.iinfo(activation_dtype).max

        self._activation_scale = nn.Parameter(None, requires_grad=False)
        self._activation_offset = nn.Parameter(None, requires_grad=False)


    @torch.no_grad()
    def set_activation_tensor_assym_scale_offset(self, activation):
        """
        This class computes the statistics for assymetric quantization of tensors for the next layer of neural network.

        Parameters
        ----------
        activation : torch.Tensor,
        """
        self.d_min = min(activation.min(), self.d_min) if self.d_min is not None\
                     else activation.min()
        self.d_max = max(activation.max(), self.d_max) if self.d_max is not None\
                     else activation.max()
        _activation_scale = (self.d_max - self.d_min)/(self.q_max - self.q_min)
        _activation_offset = self.q_min - (self.d_min/_activation_scale)
        self._activation_scale = nn.Parameter(
            _activation_scale,requires_grad=False
        )
        self._activation_offset = nn.Parameter(
            _activation_offset,requires_grad=False
        )

    @torch.no_grad()
    def compile(self):
        """
        Function that deletes temp. variables used for quantization.
        """
        self._computed = True
        del self.q_min
        del self.q_max
        del self.d_max
        del self.d_min

class QInput(Q):

    def __init__(self, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Block for quantization of input tensor.

        Parameters
        ----------
        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__(weight_dtype, activation_dtype)
    
    @torch.no_grad()
    def forward(self, X):
        if not self._computed:
            activation = X.dequantize()
            self.set_activation_tensor_assym_scale_offset(activation)

        return torch.quantize_per_tensor(
                X, self._activation_scale, self._activation_offset, self.activation_dtype
        )

class QAvgPool2d(nn.Module):
    
    def __init__(self, AvgPool2d, *args, **kwargs):
        """
        Quantized nn.AvgPool2d

        Parameters
        ----------
        AvgPool2d : nn.AvgPool2d,
        """
        super().__init__()
        self.pool_kwargs = {
            "kernel_size":AvgPool2d.kernel_size,
            "stride":AvgPool2d.stride,
            "padding":AvgPool2d.padding,
        }

    @torch.no_grad()
    def forward(self, X):
        return qF.avg_pool2d(X, **self.pool_kwargs)

class QLinear(Q):

    def __init__(self, Linear, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Quantized nn.Linear

        Parameters
        ----------
        Linear : nn.Linear,

        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__(weight_dtype, activation_dtype)
        self.Linear = Linear

        w = Linear.weight.data

        _weight_scale, _weight_offset = get_tensor_assym_scale_offset(
            w, self.weight_dtype
        )

        self.w  = nn.Parameter(
            torch.quantize_per_tensor(
                w, _weight_scale, _weight_offset, 
                self.weight_dtype
            ),
            requires_grad=False
        )

        self.b =  nn.Parameter(
            Linear.bias.data,
            requires_grad=False,
        )

    @torch.no_grad()
    def forward(self, X):
        if not self._computed:
            activation = self.Linear(X.dequantize())
            self.set_activation_tensor_assym_scale_offset(activation)
            
        out = qF.linear(
            input=X,
            weight=self.w,
            bias=self.b,
            scale=self._activation_scale,
            zero_point=self._activation_offset
        )
        
        return out

    @torch.no_grad()
    def compile(self):
        super().compile()
        del self.Linear

class QConv2d(Q):

    def __init__(self, Conv2d, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Quantized nn.Conv2d

        Parameters
        ----------
        Conv2d : nn.Conv2d,

        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__(weight_dtype, activation_dtype)

        self.Conv2d = Conv2d
        Conv_W = Conv2d.weight.data

        _weight_scale, _weight_offset = get_tensor_assym_scale_offset(
            Conv_W, self.weight_dtype
        )

        self.w = nn.Parameter(
            torch.quantize_per_tensor(
                Conv_W, _weight_scale, _weight_offset, 
                self.weight_dtype
            ),
            requires_grad=False
        )

        self.conv_kwargs = {
            "stride":Conv2d.stride,
            "padding":Conv2d.padding,
        }

    @torch.no_grad()
    def forward(self, X):

        if not self._computed:
            self.set_activation_tensor_assym_scale_offset(X)
            
        out = qF.conv2d(
            input=X,
            weight=self.w,
            bias=None,
            scale=self._activation_scale,
            zero_point=self._activation_offset,
            dtype=self.weight_dtype,
            **self.conv_kwargs
        )
        return out

    @torch.no_grad()
    def compile(self):
        super().compile()
        del self.Conv2d

class QConvBatch2d(QConv2d):

    def __init__(self, Conv2d, BatchNorm, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Quantized nn.Conv2d merged with BatchNorm2d.

        Parameters
        ----------
        Conv2d : nn.Conv2d,

        BatchNorm : nn.BatchNorm,

        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__(Conv2d, weight_dtype, activation_dtype)
        self.Conv2d = Conv2d
        self.BatchNorm = BatchNorm
        conv_W = Conv2d.weight.data
        bn_W = BatchNorm.weight.data
        bn_var = BatchNorm.running_var.data
        bn_b = BatchNorm.bias.data
        bn_mean = BatchNorm.running_mean.data
        bn_eps = BatchNorm.eps

        w = (bn_W[:, None, None, None]*conv_W)/torch.sqrt(bn_var[:, None, None, None] + bn_eps)
        
        _weight_scale, _weight_offset = get_tensor_assym_scale_offset(
            w, self.weight_dtype
        )

        self.w  = nn.Parameter(
            torch.quantize_per_tensor(
                w, _weight_scale, _weight_offset, 
                self.weight_dtype
            ),
            requires_grad=False
        )
        
        self.b =  nn.Parameter(
            bn_b - (bn_W*bn_mean)/torch.sqrt(bn_var + bn_eps),
            requires_grad=False,
        )


    @torch.no_grad()
    def forward(self, X):

        if not self._computed:
            activation = self.BatchNorm(self.Conv2d(X.dequantize()))
            self.set_activation_tensor_assym_scale_offset(activation)
            
        out = qF.conv2d(
            input=X,
            weight=self.w,
            bias=self.b,
            scale=self._activation_scale,
            zero_point=self._activation_offset,
            dtype=self.weight_dtype,
            **self.conv_kwargs
        )
        return out

    @torch.no_grad()
    def compile(self):
        super().compile()
        del self.BatchNorm

class QConvBatchReLU2d(QConvBatch2d):

    def __init__(self, Conv2d, BatchNorm, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Quantized nn.Conv2d merged with BatchNorm2d and ReLU.

        Parameters
        ----------
        Conv2d : nn.Conv2d,

        BatchNorm : nn.BatchNorm,

        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__(Conv2d, BatchNorm, weight_dtype, activation_dtype)

    @torch.no_grad()
    def forward(self, X):
        return torch.nn.functional.relu(super().forward(X))

class QFlatten(nn.Module):

    def __init__(self,  *args, **kwargs):
        """
        Quantized nn.Flatten
        """
        super().__init__()
        
    @torch.no_grad()
    def forward(self, X):
        return torch.flatten(X, 1, -1)
        
class QSkipConnection(Q):

    def __init__(self, SkipConnection, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
        """
        Quantized SkipConnection from ResNet architecture.

        Parameters
        ----------
        SkipConnection : nn.Conv2d,

        weight_dtype : torch.dtype, (default = torch.qint8)

        activation_dtype : torch.dtype, (default = torch.quint8)
        """
        super().__init__(weight_dtype, activation_dtype)
        self.f_o = SkipConnection.f
        self.c_o = SkipConnection.c
        self.f = quantize_merge_model(SkipConnection.f, quantize_wrap=False)
        self.c = quantize_merge_model(SkipConnection.c, quantize_wrap=False)
        
    @torch.no_grad()
    def forward(self, X):
        if not self._computed:
            self.set_activation_tensor_assym_scale_offset(
                torch.relu(self.c_o(X.dequantize()) +self.f_o(X.dequantize()))
            )

        return torch.ops.quantized.add_relu(self.c(X), self.f(X),
         self._activation_scale, self._activation_offset)

    @torch.no_grad()
    def compile(self):
        super().compile()
        del self.f_o
        del self.c_o

map_to_q = {
    "Conv2d":QConv2d,
    "ConvBatch2d":QConvBatch2d,
    "ConvBatchReLU2d":QConvBatchReLU2d,
    "SkipConnection":QSkipConnection,
    "AvgPool2d":QAvgPool2d,
    "Flatten":QFlatten,
    "Linear":QLinear
}

def compile_module(m):
    """
    Calling to the compile method of all subblocks of quantized network.
    
    Parameters
    ----------
    m : nn.Module,
    """
    if hasattr(m, "compile"):
        m.compile()

@torch.no_grad()
def get_tensor_assym_scale_offset(data, dtype):
    """
    Computes the statistics for assymetric quantization of tensor.

    Parameters
    ----------
    data : torch.Tensor,

    dtype : torch.dtype,
    """
    d_min = data.min()
    d_max = data.max()
    q_min = torch.iinfo(dtype).min
    q_max = torch.iinfo(dtype).max
    scale = (d_max - d_min)/(q_max - q_min)
    offset = q_min - (d_min/scale)
    return scale, offset

def quantize_merge_model(model, quantize_wrap=True, weight_dtype=torch.qint8, activation_dtype=torch.quint8):
    """
    Quantized neural network.

    Parameters
    ----------
    model : torch.Tensor,

    quantize_wrap : bool, (default=True)

    weight_dtype : torch.dtype, (default = torch.qint8)

    activation_dtype : torch.dtype, (default = torch.quint8)
    """
    qmodels = []

    if quantize_wrap:
        qmodels += [QInput()]

    if model._get_name() == "Identity":
        return nn.Identity()
    n_modules = len(model)
    k = 0

    while k < n_modules:
        if model[k]._get_name() == "Conv2d":
            if k+2 < n_modules:
                if model[k+2]._get_name() == "ReLU" and\
                model[k+1]._get_name() == "BatchNorm2d":
                    qmodels.append(
                        map_to_q["ConvBatchReLU2d"](model[k], model[k+1], weight_dtype, activation_dtype)
                    )
                    k+=3
            elif k+1 < n_modules:
                if model[k+1]._get_name() == "BatchNorm2d":
                    qmodels.append(
                        map_to_q["ConvBatch2d"](model[k], model[k+1], weight_dtype, activation_dtype)
                        )
                    k+=2
        else:
            qmodels.append(map_to_q[model[k]._get_name()](model[k], weight_dtype, activation_dtype))
            k+=1

    if quantize_wrap:
        qmodels += [DeQuantize()]
    return nn.Sequential(*qmodels)
