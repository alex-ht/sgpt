import torch
from torch import nn, Tensor
from typing import Iterable, Dict
from ..SentenceTransformer import SentenceTransformer
from .. import util
from ..util import mismatched_sizes_all_gather
from typing import List, Union
from torch import nn, Tensor
from torch.cuda.amp import GradScaler, autocast
from contextlib import nullcontext
from grad_cache.context_managers import RandContext


class MultipleNegativesRankingLoss(nn.Module):
    """
        This loss expects as input a batch consisting of sentence pairs (a_1, p_1), (a_2, p_2)..., (a_n, p_n)
        where we assume that (a_i, p_i) are a positive pair and (a_i, p_j) for i!=j a negative pair.

        For each a_i, it uses all other p_j as negative samples, i.e., for a_i, we have 1 positive example (p_i) and
        n-1 negative examples (p_j). It then minimizes the negative log-likehood for softmax normalized scores.

        This loss function works great to train embeddings for retrieval setups where you have positive pairs (e.g. (query, relevant_doc))
        as it will sample in each batch n-1 negative docs randomly.

        The performance usually increases with increasing batch sizes.

        For more information, see: https://arxiv.org/pdf/1705.00652.pdf
        (Efficient Natural Language Response Suggestion for Smart Reply, Section 4.4)

        You can also provide one or multiple hard negatives per anchor-positive pair by structering the data like this:
        (a_1, p_1, n_1), (a_2, p_2, n_2)

        Here, n_1 is a hard negative for (a_1, p_1). The loss will use for the pair (a_i, p_i) all p_j (j!=i) and all n_j as negatives.

        Example::

            from sentence_transformers import SentenceTransformer, losses, InputExample
            from torch.utils.data import DataLoader

            model = SentenceTransformer('distilbert-base-uncased')
            train_examples = [InputExample(texts=['Anchor 1', 'Positive 1']),
                InputExample(texts=['Anchor 2', 'Positive 2'])]
            train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=32)
            train_loss = losses.MultipleNegativesRankingLoss(model=model)
    """
    def __init__(self, model: SentenceTransformer, scale: float = 20.0, similarity_fct = util.cos_sim):
        """
        :param model: SentenceTransformer model
        :param scale: Output of similarity function is multiplied by scale value
        :param similarity_fct: similarity function between sentence embeddings. By default, cos_sim. Can also be set to dot product (and then set scale to 1)
        """
        super(MultipleNegativesRankingLoss, self).__init__()
        self.model = model
        self.scale = scale
        self.similarity_fct = similarity_fct
        self.cross_entropy_loss = nn.CrossEntropyLoss()

    def forward(self, sentence_features: Iterable[Dict[str, Tensor]], labels: Tensor):
        if 'token_type_ids' in sentence_features[0].keys():
            for i in range(len(sentence_features)):
                del sentence_features[i]['token_type_ids']
        reps = [self.model(sentence_feature)['sentence_embedding'] for sentence_feature in sentence_features]
        embeddings_a = reps[0]

        if torch.distributed.is_initialized():

            embeddings_b = reps[1]
            if len(reps) > 2:
                embeddings_n = torch.cat(reps[2:])
            else:
                embeddings_n = embeddings_b[:0, :]
            full_embeddings_b = mismatched_sizes_all_gather(embeddings_b)
            full_embeddings_b = torch.cat(full_embeddings_b)
            full_embeddings_n = mismatched_sizes_all_gather(embeddings_n)
            full_embeddings_n = torch.cat(full_embeddings_n)
            candidates = torch.cat([full_embeddings_b, full_embeddings_n])

            scores = self.similarity_fct(embeddings_a, candidates) * self.scale
            labels = torch.tensor(range(len(scores)), dtype=torch.long, device=scores.device)\
                     + len(scores) * torch.distributed.get_rank()
            return self.cross_entropy_loss(scores, labels)

        else:
            candidates = torch.cat(reps[1:])
            scores = self.similarity_fct(embeddings_a, candidates) * self.scale
            labels = torch.tensor(range(len(scores)), dtype=torch.long,
                                  device=scores.device)  # Example a[i] should match with b[i]
            return self.cross_entropy_loss(scores, labels)

    def get_config_dict(self):
        return {'scale': self.scale, 'similarity_fct': self.similarity_fct.__name__}

from collections import UserDict
from grad_cache import GradCache

class MNRLGradCache(GradCache):
    """
    If you use mixed precision / DeepSpeed in accelerator,
    should overwrite build_cache & forward_backward funcs to place in accelerator.backward(loss)
    """

    def __init__(self, model: SentenceTransformer, scale: float = 20.0, similarity_fct = util.cos_sim, chunk_size = 1, fp16=False, accelerator=None):
        """
        chunk_size: Final batch size bottlenecking memory, i.e. set the batch size to the actual batch size you want,
            then set chunk_size to be so small that it works
        """
        self.model = model
        self.scale = scale
        self.similarity_fct = similarity_fct
        self.cross_entropy_loss = nn.CrossEntropyLoss()
        self.is_deepspeed = True
        self.accelerator = accelerator

        # Three times the same model for three model inputs:
        # entail_a (pos), entail_b (pos), contradict (hard negative)
        # No support for asym models
        super().__init__(  
            models=[self.model, self.model, self.model],
            chunk_sizes=chunk_size,  
            loss_fn=self.loss_fn,
            split_input_fn=None,  # Should be able to handle dict of tensors
            get_rep_fn=lambda v: v["sentence_embedding"],  
            fp16=fp16,
            scaler=GradScaler(init_scale=2**12, growth_interval=500) if fp16 else None,
        )

    def loss_fn(self, embeddings_a, embeddings_b, embeddings_n=None):
        if torch.distributed.is_initialized():
            if embeddings_n is not None:
                embeddings_n = torch.cat([embeddings_n])
            else:
                embeddings_n = embeddings_b[:0, :]
            full_embeddings_b = mismatched_sizes_all_gather(embeddings_b)
            full_embeddings_b = torch.cat(full_embeddings_b)
            full_embeddings_n = mismatched_sizes_all_gather(embeddings_n)
            full_embeddings_n = torch.cat(full_embeddings_n)
            candidates = torch.cat([full_embeddings_b, full_embeddings_n])

            scores = self.similarity_fct(embeddings_a, candidates) * self.scale
            # Because we do not gather embeddings_a (i.e. we only have part of them)
            # we need to move the labels to the corresponding part of embeddings_a we have
            # i.e. on rank 0, it the first len(scores); on rank 1 it's the second len(scores),
            # so we add len(scores) * 1
            labels = torch.tensor(range(len(scores)), dtype=torch.long, device=scores.device)\
                        + len(scores) * torch.distributed.get_rank()
            return self.cross_entropy_loss(scores, labels)

        else:
            if embeddings_n is not None:
                candidates = torch.cat([embeddings_b, embeddings_n])
            else:
                candidates = torch.cat([embeddings_b])
            scores = self.similarity_fct(embeddings_a, candidates) * self.scale
            labels = torch.tensor(range(len(scores)), dtype=torch.long,
                                    device=scores.device)  # Example a[i] should match with b[i]
            return self.cross_entropy_loss(scores, labels)
    
    def __call__(self, sentence_features: Iterable[Dict[str, Tensor]], labels: Tensor):
        # e.g. for NLI, sentence_features is [emb_a, emb_b, emb_n], where
        # emb_a & emb_b have an entailment relation; emb_n is a contradiction
        # Note that each of those is a batch consisting of multiple emb_a's
        no_sync_except_last = True if torch.distributed.is_initialized() else False
        if self.is_deepspeed:
            no_sync_except_last = False
        assert self.is_deepspeed == True
        assert no_sync_except_last == False
        return super().__call__(*sentence_features, no_sync_except_last=no_sync_except_last)

    def model_call(self, model: nn.Module, model_input):
        """
        Overwrite GradCache model_call method, as we require non-extracted dict as model input
        """
        assert isinstance(model_input, (dict, UserDict)), f"Got type {type(model_input)}, but expected Dict."
        if 'token_type_ids' in model_input.keys():
            del model_input['token_type_ids']
        return model(model_input)

    def build_cache(self, *reps: Tensor, **loss_kwargs) -> Union[List[Tensor], Tensor]:
        """
        Compute the gradient cache
        :param reps: Computed representations from all encoder models
        :param loss_kwargs: Extra keyword arguments to the loss function
        :return: A tuple of a) gradient cache for each encoder model, and b) loss tensor
        """
        reps = [r.detach().requires_grad_() for r in reps]
        with autocast() if self.fp16 else nullcontext():
            loss = self.compute_loss(*reps, **loss_kwargs)

        if self.fp16:
            if self.accelerator:
                loss = self.scaler.scale(loss)
                self.accelerator.backward(loss)
            else:
                self.scaler.scale(loss).backward()
        else:
            if self.accelerator:
                self.accelerator.backward(loss)
            else:
                loss.backward()

        cache = [r.grad for r in reps]

        return cache, loss.detach()

    def forward_backward(
            self,
            model: nn.Module,
            model_inputs,
            cached_gradients: List[Tensor],
            random_states: List[RandContext],
            no_sync_except_last: bool = False
    ):
        """
        Run the second forward and the backward pass to compute gradient for a model.
        :param model: Encoder model.
        :param model_inputs: Chunked input to the encoder model.
        :param cached_gradients: Chunked gradient cache tensor for each input.
        :param random_states: Each input's device random state during the first forward.
        :param no_sync_except_last: If True, under distributed setup, only trigger gradient reduction across processes
        for the last sub-batch's forward-backward pass.
        """
        if no_sync_except_last:
            sync_contexts = [model.no_sync for _ in range(len(model_inputs) - 1)] + [nullcontext]
        else:
            sync_contexts = [nullcontext for _ in range(len(model_inputs))]

        for x, state, gradient, sync_context in zip(model_inputs, random_states, cached_gradients, sync_contexts):
            with sync_context():
                with state:
                    y = self.model_call(model, x)
                reps = self.get_reps(y)

                surrogate = torch.dot(reps.flatten(), gradient.flatten())
                if self.accelerator:
                    self.accelerator.backward(surrogate)
                else:
                    surrogate.backward()