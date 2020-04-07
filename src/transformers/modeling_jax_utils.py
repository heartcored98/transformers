import os

from flax.serialization import from_bytes

from transformers import PretrainedConfig, logger, BertConfig
from transformers.file_utils import hf_bucket_url, cached_path, WEIGHTS_NAME, TF2_WEIGHTS_NAME, TF_WEIGHTS_NAME, \
    is_remote_url


class JaxPreTrainedModel:
    config_class = None
    pretrained_model_archive_map = {}
    base_model_prefix = ""

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, *model_args, **kwargs):
        r"""
        Instantiate a pretrained pytorch model from a pre-trained model configuration.
        """
        config = kwargs.pop("config", None)
        state_dict = kwargs.pop("state_dict", None)
        cache_dir = kwargs.pop("cache_dir", None)
        from_tf = kwargs.pop("from_tf", False)
        force_download = kwargs.pop("force_download", False)
        resume_download = kwargs.pop("resume_download", False)
        proxies = kwargs.pop("proxies", None)
        output_loading_info = kwargs.pop("output_loading_info", False)
        local_files_only = kwargs.pop("local_files_only", False)

        # Load config if we don't provide a configuration
        if not isinstance(config, PretrainedConfig):
            config_path = config if config is not None else pretrained_model_name_or_path
            config, model_kwargs = cls.config_class.from_pretrained(
                config_path,
                *model_args,
                cache_dir=cache_dir,
                return_unused_kwargs=True,
                force_download=force_download,
                resume_download=resume_download,
                proxies=proxies,
                local_files_only=local_files_only,
                **kwargs,
            )
        else:
            model_kwargs = kwargs

        # Load model
        if pretrained_model_name_or_path is not None:
            if pretrained_model_name_or_path in cls.pretrained_model_archive_map:
                archive_file = cls.pretrained_model_archive_map[pretrained_model_name_or_path]
            elif os.path.isdir(pretrained_model_name_or_path):
                if from_tf and os.path.isfile(os.path.join(pretrained_model_name_or_path, TF_WEIGHTS_NAME + ".index")):
                    # Load from a TF 1.0 checkpoint
                    archive_file = os.path.join(pretrained_model_name_or_path, TF_WEIGHTS_NAME + ".index")
                elif from_tf and os.path.isfile(os.path.join(pretrained_model_name_or_path, TF2_WEIGHTS_NAME)):
                    # Load from a TF 2.0 checkpoint
                    archive_file = os.path.join(pretrained_model_name_or_path, TF2_WEIGHTS_NAME)
                elif os.path.isfile(os.path.join(pretrained_model_name_or_path, WEIGHTS_NAME)):
                    # Load from a PyTorch checkpoint
                    archive_file = os.path.join(pretrained_model_name_or_path, WEIGHTS_NAME)
                else:
                    raise EnvironmentError(
                        "Error no file named {} found in directory {} or `from_tf` set to False".format(
                            [WEIGHTS_NAME, TF2_WEIGHTS_NAME, TF_WEIGHTS_NAME + ".index"], pretrained_model_name_or_path
                        )
                    )
            elif os.path.isfile(pretrained_model_name_or_path) or is_remote_url(pretrained_model_name_or_path):
                archive_file = pretrained_model_name_or_path
            elif os.path.isfile(pretrained_model_name_or_path + ".index"):
                assert (
                    from_tf
                ), "We found a TensorFlow checkpoint at {}, please set from_tf to True to load from this checkpoint".format(
                    pretrained_model_name_or_path + ".index"
                )
                archive_file = pretrained_model_name_or_path + ".index"
            else:
                archive_file = hf_bucket_url(
                    pretrained_model_name_or_path, postfix=(TF2_WEIGHTS_NAME if from_tf else WEIGHTS_NAME)
                )

            # redirect to the cache, if necessary
            try:
                resolved_archive_file = cached_path(
                    archive_file,
                    cache_dir=cache_dir,
                    force_download=force_download,
                    proxies=proxies,
                    resume_download=resume_download,
                    local_files_only=local_files_only,
                )
            except EnvironmentError:
                if pretrained_model_name_or_path in cls.pretrained_model_archive_map:
                    msg = "Couldn't reach server at '{}' to download pretrained weights.".format(archive_file)
                else:
                    msg = (
                        "Model name '{}' was not found in model name list ({}). "
                        "We assumed '{}' was a path or url to model weight files but "
                        "couldn't find any such file at this path or url.".format(
                            pretrained_model_name_or_path,
                            ", ".join(cls.pretrained_model_archive_map.keys()),
                            archive_file,
                            [WEIGHTS_NAME, TF2_WEIGHTS_NAME, TF_WEIGHTS_NAME],
                        )
                    )
                raise EnvironmentError(msg)

            if resolved_archive_file == archive_file:
                logger.info("loading weights file {}".format(archive_file))
            else:
                logger.info("loading weights file {} from cache at {}".format(archive_file, resolved_archive_file))
        else:
            resolved_archive_file = None

        # Instantiate model.
        with open(resolved_archive_file, 'rb') as state_f:
            state_data = state_f.read()
            state = from_bytes(cls.MODEL_CLASS, state_data)["params"]
        model = cls(config, state, *model_args, **model_kwargs)
        return model


def load_pytorch_weights_in_jax_model(pt_state_dict, config: BertConfig):
    from flax.traverse_util import unflatten_dict
    state = {k: v.numpy() for k, v in pt_state_dict.items()}
    jax_state = dict(state)

    # Need to change some parameters name to match Flax names so that we don't have to fork any layer
    for key, tensor in state.items():
        # Key parts
        key_parts = set(key.split("."))

        # Every dense layer have a "kernel" parameters instead of "weight"
        if "dense.weight" in key:
            del jax_state[key]
            key = key.replace("weight", "kernel")
            jax_state[key] = tensor

        # SelfAttention needs also to replace "weight" by "kernel"
        if {"query", "key", "value"} & key_parts:

            # Flax SelfAttention decomposes the heads (num_head, size // num_heads)
            if "bias" in key:
                jax_state[key] = tensor.reshape((config.num_attention_heads, -1))
            elif "weight":
                del jax_state[key]
                key = key.replace("weight", "kernel")
                tensor = tensor.reshape((config.num_attention_heads, -1, config.hidden_size)).transpose((2, 0, 1))
                jax_state[key] = tensor

        # SelfAttention output is not a separate layer, remove one nesting
        if "attention.output.dense" in key:
            del jax_state[key]
            key = key.replace("attention.output.dense", "attention.self.out")
            jax_state[key] = tensor

        if "attention.output.LayerNorm" in key:
            del jax_state[key]
            key = key.replace("attention.output.LayerNorm", "attention.layer_norm")
            jax_state[key] = tensor

        # There are some transposed parameters w.r.t their PyTorch counterpart
        if key in {"intermediate.dense.kernel", "ouput.dense.kernel"}:
            jax_state[key] = tensor.T

        # Self Attention output projection needs to be transposed
        if "out.kernel" in key:
            jax_state[key] = tensor.reshape((768, 12, 64)).transpose(1, 2, 0)

        # Pooler needs to transpose its kernel
        if "pooler.dense.kernel" in key:
            jax_state[key] = tensor.T

    # Unflatten the dictionary to load into Jax
    return unflatten_dict({tuple(k.split('.')[1:]): v for k, v in jax_state.items()})