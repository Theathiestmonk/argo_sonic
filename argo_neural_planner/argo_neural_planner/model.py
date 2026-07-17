import torch
import torch.nn as nn
import numpy as np

class Sine(nn.Module):
    def __init__(self, omega_0=30.0):
        super().__init__()
        self.omega_0 = omega_0

    def forward(self, x):
        return torch.sin(self.omega_0 * x)

class SirenLayer(nn.Module):
    def __init__(self, in_features, out_features, bias=True, is_first=False, omega_0=30.0):
        super().__init__()
        self.omega_0 = omega_0
        self.is_first = is_first
        self.in_features = in_features
        self.linear = nn.Linear(in_features, out_features, bias=bias)
        self.activation = Sine(omega_0)
        self.init_weights()

    def init_weights(self):
        with torch.no_grad():
            if self.is_first:
                self.linear.weight.uniform_(-1 / self.in_features, 1 / self.in_features)
            else:
                self.linear.weight.uniform_(-np.sqrt(6 / self.in_features) / self.omega_0, 
                                             np.sqrt(6 / self.in_features) / self.omega_0)

    def forward(self, x):
        return self.activation(self.linear(x))

class ActiveNeuralTimeField(nn.Module):
    def __init__(self, in_features=2, hidden_features=256, hidden_layers=4, out_features=1, omega_0=30.0):
        """
        Input: 2D Coordinates [x, y] relative to the target pose.
        Output: Predicted Travel Time T(x, y).
        """
        super().__init__()
        layers = []
        layers.append(SirenLayer(in_features, hidden_features, is_first=True, omega_0=omega_0))
        
        for _ in range(hidden_layers - 1):
            layers.append(SirenLayer(hidden_features, hidden_features, is_first=False, omega_0=omega_0))
            
        final_linear = nn.Linear(hidden_features, out_features)
        with torch.no_grad():
            final_linear.weight.uniform_(-np.sqrt(6 / hidden_features) / omega_0, 
                                         np.sqrt(6 / hidden_features) / omega_0)
        layers.append(final_linear)
        
        self.network = nn.Sequential(*layers)

    def forward(self, x):
        return self.network(x)