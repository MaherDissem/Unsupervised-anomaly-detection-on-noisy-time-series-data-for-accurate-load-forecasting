import logging
import os
import pickle

import numpy as np
import torch
import torch.nn.functional as F
import tqdm
from sklearn.neighbors import LocalOutlierFactor

import common
import multi_variate_gaussian
import sampler



LOGGER = logging.getLogger(__name__)


class SoftPatch(torch.nn.Module):
    def __init__(self, device):
        super(SoftPatch, self).__init__()
        self.device = device

    def load(
        self,
        feature_extractor,
        backbone,
        device,
        input_shape,
        layers_to_extract_from=("layer2", "layer2"),
        pretrain_embed_dimension=1024,
        target_embed_dimension=1024,
        patchsize=3,
        patchstride=1,
        anomaly_score_num_nn=1,
        featuresampler=sampler.ApproximateGreedyCoresetSampler(
            percentage=0.1,
            device=torch.device("cuda")
        ),
        nn_method=common.FaissNN(False, 4),
        lof_k=5,
        threshold=0.15,
        weight_method="lof",
        soft_weight_flag=True,
        **kwargs,
    ):
        self.device = device
        self.feature_extractor = feature_extractor.to(device)
        self.backbone = backbone.to(device)
        self.layers_to_extract_from = layers_to_extract_from
        self.input_shape = input_shape

        self.patch_maker = PatchMaker(patchsize, stride=patchstride)

        self.forward_modules = torch.nn.ModuleDict({})

        feature_aggregator = common.NetworkFeatureAggregator(
            self.backbone, self.layers_to_extract_from, self.device
        )
        feature_dimensions = feature_aggregator.feature_dimensions(input_shape)
        self.forward_modules["feature_aggregator"] = feature_aggregator

        preprocessing = common.Preprocessing(
            feature_dimensions, pretrain_embed_dimension
        )
        self.forward_modules["preprocessing"] = preprocessing

        self.target_embed_dimension = target_embed_dimension
        preadapt_aggregator = common.Aggregator(
            target_dim=target_embed_dimension
        )

        _ = preadapt_aggregator.to(self.device)

        self.forward_modules["preadapt_aggregator"] = preadapt_aggregator

        self.anomaly_scorer = common.NearestNeighbourScorer(
            n_nearest_neighbours=anomaly_score_num_nn, nn_method=nn_method
        )

        self.featuresampler = featuresampler

        #------ SoftPatch ------#
        self.featuresampler = sampler.WeightedGreedyCoresetSampler(featuresampler.percentage,
                                                                   featuresampler.device)
        self.patch_weight = None
        self.feature_shape = []
        self.lof_k = lof_k
        self.threshold = threshold
        self.coreset_weight = None
        self.weight_method = weight_method
        self.soft_weight_flag = soft_weight_flag

    def embed(self, data):
        if isinstance(data, torch.utils.data.DataLoader):
            features = []
            for timeserie in data:
                if isinstance(timeserie, dict):
                    timeserie = timeserie["timeserie"]
                with torch.no_grad():
                    input_timeserie = timeserie.to(torch.float).to(self.device)
                    features.append(self._embed(input_timeserie))
            return features
        return self._embed(data)

    def _embed(self, timeseries, detach=True, provide_patch_shapes=False):
        """Returns feature embeddings for timeseries."""

        def _detach(features):
            if detach:
                return [x.detach().cpu().numpy() for x in features]
            return features
        
        # input timeseries.shape => torch.Size([8, 240, 1]), batch of 8 timeseries
        timeseries = self.feature_extractor.vectorize(timeseries.to(self.device)) # timeseries.shape -> torch.Size([8, 3, 240, 1]), batch of 8 timeseries
        
        _ = self.forward_modules["feature_aggregator"].eval()
        with torch.no_grad():
            features = self.forward_modules["feature_aggregator"](timeseries) # {layer2:torch.Size([8, 512, 28, 28]), layer3:torch.Size([8, 1024, 14, 14])}

        features = [features[layer] for layer in self.layers_to_extract_from] # [torch.Size([8, 512, 28, 28]), torch.Size([8, 1024, 14, 14])]

        features = [
            self.patch_maker.patchify(x, return_spatial_info=True) for x in features
        ]
        # features => [ ( torch.Size([8, 784, 512, 3, 3]), [28,28] ), (#layer3), ... ]
        patch_shapes = [x[1] for x in features] # [[28, 28], [14, 14], ...]
        features = [x[0] for x in features] # [torch.Size([8, 784, 512, 3, 3]), torch.Size([8, 196, 1024, 3, 3]), ...]
        ref_num_patches = patch_shapes[0]

        for i in range(1, len(features)): # for extracted layers # make all exracted layer features have same shape as first layer extracted features
            _features = features[i] # torch.Size([8, 196, 1024, 3, 3]), 196=14*14
            patch_dims = patch_shapes[i]

            _features = _features.reshape(
                _features.shape[0], patch_dims[0], patch_dims[1], *_features.shape[2:]
            ) # torch.Size([8, 14, 14, 1024, 3, 3])
            _features = _features.permute(0, -3, -2, -1, 1, 2) # torch.Size([8, 1024, 3, 3, 14, 14])
            perm_base_shape = _features.shape
            _features = _features.reshape(-1, *_features.shape[-2:]) # torch.Size([73728, 14, 14]), 73728=8*1024*3*3
            _features = F.interpolate(
                _features.unsqueeze(1),
                size=(ref_num_patches[0], ref_num_patches[1]),
                mode="bilinear",
                align_corners=False,
            ) # torch.Size([73728, 1, 28, 28])
            _features = _features.squeeze(1) # torch.Size([73728, 28, 28])
            _features = _features.reshape(
                *perm_base_shape[:-2], ref_num_patches[0], ref_num_patches[1]
            ) # torch.Size([8, 1024, 3, 3, 28, 28])
            _features = _features.permute(0, -2, -1, 1, 2, 3) # torch.Size([8, 28, 28, 1024, 3, 3])
            _features = _features.reshape(len(_features), -1, *_features.shape[-3:]) # torch.Size([8, 784, 1024, 3, 3])
            features[i] = _features
        features = [x.reshape(-1, *x.shape[-3:]) for x in features] # [torch.Size([6272, 512, 3, 3]), torch.Size([6272, 1024, 3, 3]), ], 6272=8*784

        # As different feature backbones & patching provide differently
        # sized features, these are brought into the correct form here.
        features = self.forward_modules["preprocessing"](features) # torch.Size([6272, 2, 1024])
        features = self.forward_modules["preadapt_aggregator"](features) # torch.Size([6272, 1024]), 6272=8*28*28

        if provide_patch_shapes:
            return _detach(features), patch_shapes
        return _detach(features)

    def fit(self, training_data):
        """
        This function computes the embeddings of the training data and fills the
        memory bank of SPADE.
        """
        self._fill_memory_bank(training_data)

    def _fill_memory_bank(self, input_data):
        """Computes and sets the support features for SPADE."""
        _ = self.forward_modules.eval()

        def _timeserie_to_features(input_timeserie):
            with torch.no_grad():
                input_timeserie = input_timeserie.to(torch.float).to(self.device)
                return self._embed(input_timeserie)

        features = []
        with tqdm.tqdm(
            input_data, desc="Computing support features...", leave=True
        ) as data_iterator:
            for timeserie in data_iterator:
                if isinstance(timeserie, dict):
                    timeserie = timeserie["data"]
                features.append(_timeserie_to_features(timeserie))

        features = np.concatenate(features, axis=0)

        with torch.no_grad():
            self.feature_shape = self._embed(timeserie.to(torch.float).to(self.device), provide_patch_shapes=True)[1][0]
            patch_weight = self._compute_patch_weight(features)

            patch_weight = patch_weight.reshape(-1)
            threshold = torch.quantile(patch_weight, 1 - self.threshold)
            sampling_weight = torch.where(patch_weight > threshold, 0, 1) # denoising: sampling_weight[i] is 0 if patch_weight[i] > threshold, else 1, i.e. high weight are ignored
            self.featuresampler.set_sampling_weight(sampling_weight)
            self.patch_weight = patch_weight.clamp(min=0)

            sample_features, sample_indices = self.featuresampler.run(features) 
            self.coreset_weight = self.patch_weight[sample_indices].cpu().numpy()

        self.anomaly_scorer.fit(detection_features=[sample_features])

    def _compute_patch_weight(self, features: np.ndarray):
        if isinstance(features, np.ndarray):
            features = torch.from_numpy(features)

        reduced_features = self.featuresampler._reduce_features(features)
        patch_features = \
            reduced_features.reshape(-1, self.feature_shape[0]*self.feature_shape[1], reduced_features.shape[-1])

        patch_features = patch_features.permute(1, 0, 2)

        if self.weight_method == "lof":
            patch_weight = self._compute_lof(self.lof_k, patch_features).transpose(-1, -2) 
        elif self.weight_method == "lof_gpu":
            patch_weight = self._compute_lof_gpu(self.lof_k, patch_features).transpose(-1, -2)
        elif self.weight_method == "nearest":
            patch_weight = self._compute_nearest_distance(patch_features).transpose(-1, -2)
            patch_weight = patch_weight + 1
        elif self.weight_method == "gaussian":
            gaussian = multi_variate_gaussian.MultiVariateGaussian(patch_features.shape[2], patch_features.shape[0])
            stats = gaussian.fit(patch_features)
            patch_weight = self._compute_distance_with_gaussian(patch_features, stats).transpose(-1, -2)
            patch_weight = patch_weight + 1
        else:
            raise ValueError("Unexpected weight method")

        return patch_weight

    def _compute_distance_with_gaussian(self, embedding: torch.Tensor, stats: [torch.Tensor]) -> torch.Tensor:
        """
        Args:
            embedding (Tensor): Embedding Vector
            stats (List[Tensor]): Mean and Covariance Matrix of the multivariate Gaussian distribution

        Returns:
            Anomaly score of a test timeserie via mahalanobis distance.
        """
        # patch, batch, channel = embedding.shape
        embedding = embedding.permute(1, 2, 0)

        mean, inv_covariance = stats
        delta = (embedding - mean).permute(2, 0, 1)

        distances = (torch.matmul(delta, inv_covariance) * delta).sum(2)
        distances = torch.sqrt(distances)

        return distances

    def _compute_nearest_distance(self, embedding: torch.Tensor) -> torch.Tensor:
        patch, batch, _ = embedding.shape

        x_x = (embedding ** 2).sum(dim=-1, keepdim=True).expand(patch, batch, batch)
        dist_mat = (x_x + x_x.transpose(-1, -2) - 2 * embedding.matmul(embedding.transpose(-1, -2))).abs() ** 0.5
        nearest_distance = torch.topk(dist_mat, dim=-1, largest=False, k=2)[0].sum(dim=-1)  #
        return nearest_distance

    def _compute_lof(self, k, embedding: torch.Tensor) -> torch.Tensor:
        patch, batch, _ = embedding.shape   # 784x219x128
        clf = LocalOutlierFactor(n_neighbors=int(k), metric='l2')
        scores = torch.zeros(size=(patch, batch), device=embedding.device)
        for i in range(patch):
            clf.fit(embedding[i].cpu())
            scores[i] = torch.Tensor(- clf.negative_outlier_factor_)
        return scores

    def _compute_lof_gpu(self, k, embedding: torch.Tensor) -> torch.Tensor:
        """
        GPU support
        """

        patch, batch, _ = embedding.shape

        # calculate distance
        x_x = (embedding ** 2).sum(dim=-1, keepdim=True).expand(patch, batch, batch)
        dist_mat = (x_x + x_x.transpose(-1, -2) - 2 * embedding.matmul(embedding.transpose(-1, -2))).abs() ** 0.5 + 1e-6

        # find neighborhoods
        top_k_distance_mat, top_k_index = torch.topk(dist_mat, dim=-1, largest=False, k=k + 1)
        top_k_distance_mat, top_k_index = top_k_distance_mat[:, :, 1:], top_k_index[:, :, 1:]
        k_distance_value_mat = top_k_distance_mat[:, :, -1]

        # calculate reachability distance
        reach_dist_mat = torch.max(dist_mat, k_distance_value_mat.unsqueeze(2).expand(patch, batch, batch)
                                   .transpose(-1, -2))  # Transposing is important
        top_k_index_hot = torch.zeros(size=dist_mat.shape, device=top_k_index.device).scatter_(-1, top_k_index, 1)

        # Local reachability density
        lrd_mat = k / (top_k_index_hot * reach_dist_mat).sum(dim=-1)

        # calculate local outlier factor
        lof_mat = ((lrd_mat.unsqueeze(2).expand(patch, batch, batch).transpose(-1, -2) * top_k_index_hot).sum(
            dim=-1) / k) / lrd_mat
        return lof_mat


    def _chunk_lof(self, k, embedding: torch.Tensor) -> torch.Tensor:
        width, height, batch, channel = embedding.shape
        chunk_size = 2

        new_width, new_height = int(width / chunk_size), int(height / chunk_size)
        new_patch = new_width * new_height
        new_batch = batch * chunk_size * chunk_size

        split_width = torch.stack(embedding.split(chunk_size, dim=0), dim=0)
        split_height = torch.stack(split_width.split(chunk_size, dim=1 + 1), dim=1)

        new_embedding = split_height.view(new_patch, new_batch, channel)
        lof_mat = self._compute_lof(k, new_embedding)
        chunk_lof_mat = lof_mat.reshape(new_width, new_height, chunk_size, chunk_size, batch)
        chunk_lof_mat = chunk_lof_mat.transpose(1, 2).reshape(width, height, batch)
        return chunk_lof_mat


    def predict(self, data):
        if isinstance(data, torch.utils.data.DataLoader):
            return self._predict_dataloader(data)
        return self._predict(data)

    def _predict_dataloader(self, dataloader):
        """This function provides anomaly scores/maps for full dataloaders."""
        _ = self.forward_modules.eval()

        scores = []
        masks = []
        labels_gt = []
        with tqdm.tqdm(dataloader, desc="Inferring...", leave=True) as data_iterator:
            for timeserie in data_iterator: 
                if isinstance(timeserie, dict):
                    labels_gt.extend(timeserie["is_anomaly"].numpy().tolist())
                    timeserie = timeserie["data"]
                _scores, _masks = self._predict(timeserie)
                for score, mask in zip(_scores, _masks):
                    scores.append(score)
                    masks.append(mask)
        return scores, masks, labels_gt

    def _predict(self, timeseries):
        """Infer score and mask for a batch of timeseries."""
        timeseries = timeseries.to(torch.float).to(self.device)
        _ = self.forward_modules.eval()

        batchsize = timeseries.shape[0]
        with torch.no_grad():
            features, patch_shapes = self._embed(timeseries, provide_patch_shapes=True)
            features = np.asarray(features)

            timeserie_scores, _, indices = self.anomaly_scorer.predict([features])
            if self.soft_weight_flag:
                indices = indices.squeeze()
                weight = np.take(self.coreset_weight, axis=0, indices=indices)

                timeserie_scores = timeserie_scores * weight 

            patch_scores = timeserie_scores

            timeserie_scores = self.patch_maker.unpatch_scores(
                timeserie_scores, batchsize=batchsize
            )
            timeserie_scores = timeserie_scores.reshape(*timeserie_scores.shape[:2], -1)
            timeserie_scores = self.patch_maker.score(timeserie_scores)

            patch_scores = self.patch_maker.unpatch_scores(
                patch_scores, batchsize=batchsize
            )
            scales = patch_shapes[0]
            patch_scores = patch_scores.reshape(batchsize, scales[0], scales[1])
            masks = patch_scores

        return [score for score in timeserie_scores], [mask for mask in masks]

    @staticmethod
    def _params_file(filepath, prepend=""):
        return os.path.join(filepath, prepend + "params.pkl")

    def save_to_path(self, save_path: str, prepend: str = "") -> None:
        LOGGER.info("Saving data.")
        self.anomaly_scorer.save(
            save_path, save_features_separately=False, prepend=prepend
        )
        params = {
            "layers_to_extract_from": self.layers_to_extract_from,
            "input_shape": self.input_shape,
            "pretrain_embed_dimension": self.forward_modules[
                "preprocessing"
            ].output_dim,
            "target_embed_dimension": self.forward_modules[
                "preadapt_aggregator"
            ].target_dim,
            "patchsize": self.patch_maker.patchsize,
            "patchstride": self.patch_maker.stride,
            "anomaly_scorer_num_nn": self.anomaly_scorer.n_nearest_neighbours,
        }
        with open(self._params_file(save_path, prepend), "wb") as save_file:
            pickle.dump(params, save_file, pickle.HIGHEST_PROTOCOL)

    def load_from_path(
        self,
        load_path: str,
        device: torch.device,
        nn_method: common.FaissNN(False, 4),
        prepend: str = "",
    ) -> None:
        LOGGER.info("Loading and initializing.")
        with open(self._params_file(load_path, prepend), "rb") as load_file:
            params = pickle.load(load_file)
        self.load(**params, device=device, nn_method=nn_method)

        self.anomaly_scorer.load(load_path, prepend)


class PatchMaker:
    def __init__(self, patchsize, stride=None):
        self.patchsize = patchsize
        self.stride = stride

    def patchify(self, features, return_spatial_info=False):
        """Convert a tensor into a tensor of respective patches.
        Args:
            x: [torch.Tensor, bs x c x w x h]
        Returns:
            x: [torch.Tensor, bs * w//stride * h//stride, c, patchsize,
            patchsize]
        """
        padding = int((self.patchsize - 1) / 2)
        unfolder = torch.nn.Unfold(
            kernel_size=self.patchsize, stride=self.stride, padding=padding, dilation=1
        )
        unfolded_features = unfolder(features)
        number_of_total_patches = []
        for side in features.shape[-2:]:
            n_patches = (
                side + 2 * padding - 1 * (self.patchsize - 1) - 1
            ) / self.stride + 1
            number_of_total_patches.append(int(n_patches))
        unfolded_features = unfolded_features.reshape(
            *features.shape[:2], self.patchsize, self.patchsize, -1
        )
        unfolded_features = unfolded_features.permute(0, 4, 1, 2, 3)

        if return_spatial_info:
            return unfolded_features, number_of_total_patches
        return unfolded_features

    def unpatch_scores(self, patch_scores, batchsize):
        return patch_scores.reshape(batchsize, -1, *patch_scores.shape[1:])

    def score(self, timeserie_scores):
        was_numpy = False
        if isinstance(timeserie_scores, np.ndarray):
            was_numpy = True
            timeserie_scores = torch.from_numpy(timeserie_scores)
        while timeserie_scores.ndim > 1:
            timeserie_scores = torch.max(timeserie_scores, dim=-1).values
        if was_numpy:
            return timeserie_scores.numpy()
        return timeserie_scores