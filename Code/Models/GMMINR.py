from random import gauss
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from Other.utility_functions import make_coord_grid    

class LReLULayer(nn.Module):
    def __init__(self, in_features, out_features, bias=True):
        super().__init__()
        
        self.in_features = in_features
        self.linear = nn.Linear(in_features, out_features, 
            bias=bias)
        
        self.init_weights()
    
    def init_weights(self):
        with torch.no_grad():
            nn.init.kaiming_uniform_(self.linear.weight)

    def forward(self, input):
        return F.leaky_relu(self.linear(input), 0.2)

class SineLayer(nn.Module):
    def __init__(self, in_features, out_features, bias=True,
                 is_first=False, omega_0=30):
        super().__init__()
        self.omega_0 = omega_0
        self.is_first = is_first
        
        self.in_features = in_features
        self.linear = nn.Linear(in_features, out_features, 
            bias=bias)
        
        self.init_weights()
    
    def init_weights(self):
        with torch.no_grad():
            if self.is_first:
                self.linear.weight.uniform_(-1 / self.in_features, 
                                             1 / self.in_features)      
            else:
                self.linear.weight.uniform_(-np.sqrt(6 / self.in_features) / self.omega_0, 
                                             np.sqrt(6 / self.in_features) / self.omega_0)
        
    def forward(self, input):
        return torch.sin(self.omega_0 * self.linear(input))

class SnakeAltLayer(nn.Module):
    def __init__(self, in_features, out_features):
        super().__init__()
        self.linear = nn.Linear(in_features, out_features)        
        self.init_weights()
    
    def init_weights(self):
        with torch.no_grad():
            nn.init.xavier_normal_(self.linear.weight)
        
    def forward(self, input):
        x = self.linear(input)
        return 0.5*x + torch.sin(x)**2

class PositionalEncoding(nn.Module):
    def __init__(self, opt):
        super(PositionalEncoding, self).__init__()        
        self.opt = opt
        self.L = opt['num_positional_encoding_terms']
        self.L_terms = torch.arange(0, opt['num_positional_encoding_terms'], 
            device=opt['device'], dtype=torch.float32).repeat_interleave(2*opt['n_dims'])
        self.L_terms = torch.pow(2, self.L_terms) * torch.pi

    def forward(self, locations):
        repeats = len(list(locations.shape)) * [1]
        repeats[-1] = self.L*2
        locations = locations.repeat(repeats)
        
        locations = locations * self.L_terms# + self.phase_shift
        if(self.opt['n_dims'] == 2):
            locations[..., 0::4] = torch.sin(locations[..., 0::4])
            locations[..., 1::4] = torch.sin(locations[..., 1::4])
            locations[..., 2::4] = torch.cos(locations[..., 2::4])
            locations[..., 3::4] = torch.cos(locations[..., 3::4])
        else:
            locations[..., 0::6] = torch.sin(locations[..., 0::6])
            locations[..., 1::6] = torch.sin(locations[..., 1::6])
            locations[..., 2::6] = torch.sin(locations[..., 2::6])
            locations[..., 3::6] = torch.cos(locations[..., 3::6])
            locations[..., 4::6] = torch.cos(locations[..., 4::6])
            locations[..., 5::6] = torch.cos(locations[..., 5::6])
        return locations
       
class GMMINR(nn.Module):
    def __init__(self, opt):
        super().__init__()
        
        self.opt = opt
        
        # Generate random centers for the gaussians
        self.gaussian_centers = torch.nn.parameter.Parameter(
            torch.rand(
                [opt['n_gaussians'], opt['n_dims']],
                device = opt['device']
            ) * 2 - 1
        )

        # Construct a covariance matrix with Q*S*Q.T where Q is a householder matrix
        S = torch.eye(opt['n_dims'],device=opt['device']).unsqueeze(0).repeat(opt['n_gaussians'], 1, 1)
        S *= ((1/(self.opt['n_gaussians'])) if opt['n_gaussians'] > 0 else 1)
        v = torch.rand([opt['n_gaussians'], opt['n_dims'], 1],device=opt['device'])
        v /= torch.linalg.norm(v, dim=1, keepdim=True)
        Q = torch.eye(opt['n_dims'],device=opt['device']).unsqueeze(0).repeat(opt['n_gaussians'], 1, 1) - \
            2*torch.bmm(v, v.mT)
        cov = torch.bmm(torch.bmm(Q, S), Q.mT)

        # Convert to a precision matrix (stabilizes training)
        self.gaussian_precision = torch.nn.parameter.Parameter(
            torch.linalg.inv(cov) 
        ) 

        # Generate random starting features for each gaussian        
        self.gaussian_features = torch.nn.parameter.Parameter(
            torch.ones(
                [opt['n_gaussians'], opt['n_features']],
                device = opt['device']
            ).normal_(0, 1)
        )
        self.pe = PositionalEncoding(opt)
        
        self.decoder = nn.ModuleList()
        #first_layer_input_size = opt['num_positional_encoding_terms']*opt['n_dims']*2
        first_layer_input_size = opt['n_dims']
        if(self.opt['n_gaussians'] > 0):
            first_layer_input_size += opt['n_features']
        if(opt['n_layers'] > 0):
            layer = SineLayer(first_layer_input_size, 
                              opt['nodes_per_layer'], 
                              is_first=True)
            self.decoder.append(layer)
            
            for i in range(opt['n_layers']):
                if i == opt['n_layers'] - 1:
                    layer = nn.Linear(opt['nodes_per_layer'], opt['n_outputs'])
                    nn.init.xavier_normal_(layer.weight)
                    self.decoder.append(layer)
                else:
                    layer = SineLayer(opt['nodes_per_layer'], opt['nodes_per_layer'])
                    self.decoder.append(layer)
            self.decoder.append(nn.Tanh())
        else:
            layer = nn.Linear(first_layer_input_size, opt['n_outputs'])
            nn.init.kaiming_uniform_(layer.weight)
            self.decoder.append(layer)
        self.decoder = nn.Sequential(*self.decoder)
        
        self.network_parameters = [param for param in self.decoder.parameters()]
        self.network_parameters.append(self.gaussian_features)
    
    def gaussian_density(self, grid):
        
        if(self.opt['n_gaussians'] == 0):
            return torch.zeros([grid[0], grid[1], 3])

        
        x = make_coord_grid(
            grid, 
            self.opt['data_device'],
            flatten=False,
            align_corners=self.opt['align_corners'])
        x_shape = list(x.shape)
        x_shape[-1] = 1
        x = x.flatten(0,1)                
        x = x.unsqueeze(1).repeat(1,self.opt['n_gaussians'],1)
        
        coeff = 1 / (((2* np.pi)**(self.opt['n_dims']/2)) * \
            (torch.linalg.det(torch.linalg.inv(self.gaussian_precision))**(1/2)))
        exp_part =  torch.exp((-1/2) * \
            ((x-self.gaussian_centers.unsqueeze(0)).unsqueeze(-1).mT\
                .matmul(self.gaussian_precision.unsqueeze(0)))\
                    .matmul((x-self.gaussian_centers.unsqueeze(0)).unsqueeze(-1))).squeeze()
        result = coeff.unsqueeze(0) * exp_part
        
        result = result.sum(dim=1, keepdim=True).reshape(x_shape)
        result /= result.max()
        return result
        
    def forward(self, x):     
        
        #decoder_input = self.pe(x)
        decoder_input = x
        
        if(self.opt['n_gaussians'] > 0):
            
            gauss_dist = x.unsqueeze(1).repeat(1,self.opt['n_gaussians'],1)
            coeff = 1 / (((2* np.pi)**(self.opt['n_dims']/2)) * \
                (torch.linalg.det(torch.linalg.inv(self.gaussian_precision))**(1/2)))
            

            exp_part = torch.exp((-1/2) * \
                ((gauss_dist-self.gaussian_centers.unsqueeze(0)).unsqueeze(-1).mT\
                    .matmul(self.gaussian_precision.unsqueeze(0)))\
                        .matmul((gauss_dist-self.gaussian_centers.unsqueeze(0)).unsqueeze(-1))).squeeze()
            result = coeff.unsqueeze(0) * exp_part
            #print(f"result: {result.min()} {result.max()}")
            feature_vectors = torch.matmul(result.unsqueeze(1),
                            self.gaussian_features.unsqueeze(0)).squeeze()
            feature_vectors *= ((6/self.opt['n_gaussians'])**0.5)

            #print(f"feature: {self.gaussian_features[0]}")
            #print(f"mean: {self.gaussian_features.mean(dim=1)[0]}")
            #print(f"std: {self.gaussian_features.std(dim=1)[0]}")

            #print(f"exp_part: {exp_part.min():0.02f} {exp_part.max():0.02f}")
            #print(f"after exp: {torch.exp(exp_part.squeeze()).min():0.02f} {torch.exp(exp_part.squeeze()).max():0.02f}")
            decoder_input = torch.cat([feature_vectors, decoder_input], dim=1)
            
        y = self.decoder(decoder_input)   
        #print(f"y terms: {y.min():0.02f} {y.max():0.02f}")

        return y

        