import argparse
import os

import numpy as np

from ctranslate2.converters import utils
from ctranslate2.converters.converter import Converter
from ctranslate2.specs import common_spec
from ctranslate2.specs import transformer_spec


def load_model(model_path):
    """Loads variables from a TensorFlow checkpoint."""
    import tensorflow as tf

    if tf.saved_model.contains_saved_model(model_path):
        raise RuntimeError(
            "Converting the SavedModel format is not supported, "
            "please convert a TensorFlow checkpoint instead"
        )

    if os.path.isdir(model_path):
        checkpoint = tf.train.latest_checkpoint(model_path)
    else:
        checkpoint = model_path

    reader = tf.train.load_checkpoint(checkpoint)
    variables = {
        name: reader.get_tensor(name)
        for name in reader.get_variable_to_shape_map().keys()
    }

    model_version = 1
    if os.path.basename(checkpoint).startswith("ckpt"):
        model_version = 2
        variables = {
            name.replace("/.ATTRIBUTES/VARIABLE_VALUE", ""): value
            for name, value in variables.items()
        }

    return model_version, variables


def _load_vocab(vocab, unk_token="<unk>"):
    import opennmt

    if isinstance(vocab, opennmt.data.Vocab):
        tokens = list(vocab.words)
    elif isinstance(vocab, list):
        tokens = list(vocab)
    elif isinstance(vocab, str):
        tokens = opennmt.data.Vocab.from_file(vocab).words
    else:
        raise TypeError("Invalid vocabulary type")

    if unk_token not in tokens:
        tokens.append(unk_token)
    return tokens


class OpenNMTTFConverter(Converter):
    """Converts models generated by OpenNMT-tf."""

    def __init__(
        self,
        model_spec,
        src_vocab,
        tgt_vocab,
        model_path=None,
        variables=None,
    ):
        if (model_path is None) == (variables is None):
            raise ValueError("Exactly one of model_path and variables should be set")
        if variables is not None and not isinstance(variables, dict):
            raise ValueError(
                "variables should be a dict mapping variable name to value"
            )
        self._model_spec = model_spec
        self._model_path = model_path
        self._src_vocab = src_vocab
        self._tgt_vocab = tgt_vocab
        self._variables = variables

    def _load(self):
        model_spec = self._model_spec
        if self._model_path is not None:
            version, variables = load_model(self._model_path)
        else:
            version = 2  # Assume we are passing V2 variables.
            variables = self._variables
        if version >= 2:
            set_transformer_spec_v2(model_spec, variables)
        else:
            set_transformer_spec(model_spec, variables)
        model_spec.register_vocabulary("source", _load_vocab(self._src_vocab))
        model_spec.register_vocabulary("target", _load_vocab(self._tgt_vocab))
        return model_spec


def set_transformer_spec_v2(spec, variables):
    set_embeddings(
        spec.encoder.embeddings,
        variables,
        "model/examples_inputter/features_inputter",
        version=2,
    )
    try:
        target_embedding_name = set_embeddings(
            spec.decoder.embeddings,
            variables,
            "model/examples_inputter/labels_inputter",
            version=2,
        )
    except KeyError:
        target_embedding_name = set_embeddings(
            spec.decoder.embeddings,
            variables,
            "model/examples_inputter/features_inputter",
            version=2,
        )
    set_transformer_encoder_v2(
        spec.encoder, variables, "model/encoder", relative=spec.with_relative_position
    )
    set_transformer_decoder_v2(
        spec.decoder,
        variables,
        "model/decoder",
        target_embedding_name,
        relative=spec.with_relative_position,
    )


def set_transformer_encoder_v2(spec, variables, scope, relative=False):
    set_layer_norm(spec.layer_norm, variables, "%s/layer_norm" % scope)
    for i, layer in enumerate(spec.layer):
        set_transformer_encoder_layer_v2(
            layer, variables, "%s/layers/%d" % (scope, i), relative=relative
        )


def set_transformer_decoder_v2(
    spec, variables, scope, target_embedding_name, relative=False
):
    try:
        set_linear(
            spec.projection,
            variables,
            "%s/output_layer" % scope,
            transpose=False,
        )
        if not np.array_equal(spec.projection.weight, spec.embeddings.weight):
            spec.projection.weight = spec.projection.weight.transpose()
    except KeyError:
        set_linear(
            spec.projection,
            variables,
            "%s/output_layer" % scope,
            weight_name=target_embedding_name,
            transpose=False,
        )
    set_layer_norm(spec.layer_norm, variables, "%s/layer_norm" % scope)
    for i, layer in enumerate(spec.layer):
        set_transformer_decoder_layer_v2(
            layer, variables, "%s/layers/%d" % (scope, i), relative=relative
        )


def set_transformer_encoder_layer_v2(spec, variables, scope, relative=False):
    set_ffn_v2(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention_v2(
        spec.self_attention,
        variables,
        "%s/self_attention" % scope,
        self_attention=True,
        relative=relative,
    )


def set_transformer_decoder_layer_v2(spec, variables, scope, relative=False):
    set_ffn_v2(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention_v2(
        spec.self_attention,
        variables,
        "%s/self_attention" % scope,
        self_attention=True,
        relative=relative,
    )
    set_multi_head_attention_v2(
        spec.attention, variables, "%s/attention/0" % scope, relative=relative
    )


def set_ffn_v2(spec, variables, scope):
    set_layer_norm(spec.layer_norm, variables, "%s/input_layer_norm" % scope)
    set_linear(spec.linear_0, variables, "%s/layer/inner" % scope)
    set_linear(spec.linear_1, variables, "%s/layer/outer" % scope)


def set_multi_head_attention_v2(
    spec, variables, scope, self_attention=False, relative=False
):
    set_layer_norm(spec.layer_norm, variables, "%s/input_layer_norm" % scope)
    if self_attention:
        split_layers = [common_spec.LinearSpec() for _ in range(3)]
        set_linear(split_layers[0], variables, "%s/layer/linear_queries" % scope)
        set_linear(split_layers[1], variables, "%s/layer/linear_keys" % scope)
        set_linear(split_layers[2], variables, "%s/layer/linear_values" % scope)
        utils.fuse_linear(spec.linear[0], split_layers)
        if relative:
            spec.relative_position_keys = variables[
                "%s/layer/relative_position_keys" % scope
            ]
            spec.relative_position_values = variables[
                "%s/layer/relative_position_values" % scope
            ]
    else:
        set_linear(spec.linear[0], variables, "%s/layer/linear_queries" % scope)
        split_layers = [common_spec.LinearSpec() for _ in range(2)]
        set_linear(split_layers[0], variables, "%s/layer/linear_keys" % scope)
        set_linear(split_layers[1], variables, "%s/layer/linear_values" % scope)
        utils.fuse_linear(spec.linear[1], split_layers)
    set_linear(spec.linear[-1], variables, "%s/layer/linear_output" % scope)


def set_transformer_spec(spec, variables):
    if spec.with_relative_position:
        raise NotImplementedError()
    set_transformer_encoder(spec.encoder, variables)
    set_transformer_decoder(spec.decoder, variables)


def set_transformer_encoder(spec, variables):
    set_layer_norm(spec.layer_norm, variables, "transformer/encoder/LayerNorm")
    try:
        set_embeddings(spec.embeddings, variables, "transformer/encoder")
    except KeyError:
        # Try shared embeddings scope instead.
        set_embeddings(spec.embeddings, variables, "transformer/shared_embeddings")
    for i, layer in enumerate(spec.layer):
        set_transformer_encoder_layer(
            layer, variables, "transformer/encoder/layer_%d" % i
        )


def set_transformer_decoder(spec, variables):
    try:
        embeddings_name = set_embeddings(
            spec.embeddings, variables, "transformer/decoder"
        )
    except KeyError:
        # Try shared embeddings scope instead.
        embeddings_name = set_embeddings(
            spec.embeddings, variables, "transformer/shared_embeddings"
        )
    try:
        set_linear(spec.projection, variables, "transformer/decoder/dense")
    except KeyError:
        # Try reusing the target embeddings.
        set_linear(
            spec.projection,
            variables,
            "transformer",
            weight_name=embeddings_name,
            transpose=False,
        )
    set_layer_norm(spec.layer_norm, variables, "transformer/decoder/LayerNorm")
    for i, layer in enumerate(spec.layer):
        set_transformer_decoder_layer(
            layer, variables, "transformer/decoder/layer_%d" % i
        )


def set_transformer_encoder_layer(spec, variables, scope):
    set_ffn(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention(
        spec.self_attention, variables, "%s/multi_head" % scope, self_attention=True
    )


def set_transformer_decoder_layer(spec, variables, scope):
    set_ffn(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention(
        spec.self_attention,
        variables,
        "%s/masked_multi_head" % scope,
        self_attention=True,
    )
    set_multi_head_attention(spec.attention, variables, "%s/multi_head" % scope)


def set_ffn(spec, variables, scope):
    set_layer_norm(spec.layer_norm, variables, "%s/LayerNorm" % scope)
    set_linear(spec.linear_0, variables, "%s/conv1d" % scope)
    set_linear(spec.linear_1, variables, "%s/conv1d_1" % scope)


def set_multi_head_attention(spec, variables, scope, self_attention=False):
    set_layer_norm(spec.layer_norm, variables, "%s/LayerNorm" % scope)
    set_linear(spec.linear[0], variables, "%s/conv1d" % scope)
    set_linear(spec.linear[1], variables, "%s/conv1d_1" % scope)
    if not self_attention:
        set_linear(spec.linear[2], variables, "%s/conv1d_2" % scope)


def set_layer_norm(spec, variables, scope):
    spec.gamma = variables["%s/gamma" % scope]
    spec.beta = variables["%s/beta" % scope]


def set_linear(spec, variables, scope, weight_name=None, transpose=True):
    if weight_name is None:
        weight_name = "%s/kernel" % scope
    spec.weight = variables[weight_name].squeeze()
    if transpose:
        spec.weight = spec.weight.transpose()
    bias = variables.get("%s/bias" % scope)
    if bias is not None:
        spec.bias = bias


def set_embeddings(spec, variables, scope, version=1):
    if version == 2:
        name = "embedding"
    else:
        name = "w_embs"
    variable_name = "%s/%s" % (scope, name)
    spec.weight = variables[variable_name]
    spec.multiply_by_sqrt_depth = True
    return variable_name


def main():
    import opennmt

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--model_path",
        required=True,
        help="Model path (a checkpoint or a checkpoint directory).",
    )
    parser.add_argument(
        "--model_type",
        required=True,
        help="The model type used in OpenNMT-tf training.",
    )
    parser.add_argument(
        "--src_vocab",
        required=True,
        help="Source vocabulary file.",
    )
    parser.add_argument(
        "--tgt_vocab",
        required=True,
        help="Target vocabulary file.",
    )
    Converter.declare_arguments(parser)
    args = parser.parse_args()
    model = opennmt.models.get_model_from_catalog(args.model_type)
    model_spec = model.ctranslate2_spec
    if model_spec is None:
        raise NotImplementedError("Model %s is not supported" % args.model_type)
    OpenNMTTFConverter(
        model_spec,
        args.src_vocab,
        args.tgt_vocab,
        model_path=args.model_path,
    ).convert_from_args(args)


if __name__ == "__main__":
    main()
