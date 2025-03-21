import argparse
import json
import os
import time
from typing import List

import numpy as np
from gensim.models import Word2Vec

from src.finetuning import (
    run_finetuning_wkfl2,
    run_finetuning_wkfl3,
    setup_finetuning_input,
)
from src.pretraining import run_pretraining, setup_pretraining_input
from src.node2vec.src.node2vec import Graph
from src.processing.context_generator import ContextGenerator
from src.processing.generic_attributed_graph import GenericGraph
from src.utils.data_utils import load_pretrained_node2vec
from src.utils.evaluation import run_evaluation_main
from src.utils.link_predict import find_optimal_cutoff, link_prediction_eval
from src.utils.utils import get_id_map, load_pickle


def get_set_embeddings_details(args):
    if not args.pretrained_embeddings:
        if args.pretrained_method == "compgcn":
            args.pretrained_embeddings = (
                f"{args.emb_dir}/{args.data_name}/"
                f"act_{args.data_name}_{args.node_edge_composition_func}_500.out"
            )
        elif args.pretrained_method == "node2vec":
            args.base_embedding_dim = 128
            args.pretrained_embeddings = (
                f"{args.emb_dir}/{args.data_name}/{args.data_name}.emd"
            )
    else:
        if args.pretrained_method == "compgcn":
            args.base_embedding_dim = 200
        elif args.pretrained_method == "node2vec":
            args.base_embedding_dim = 128
    return args.pretrained_embeddings, args.base_embedding_dim


def get_graph(data_path, false_edge_gen):
    print("\n Loading graph...")
    attr_graph = GenericGraph(data_path, false_edge_gen)
    context_gen = ContextGenerator(attr_graph, args.num_walks_per_node)

    return attr_graph, context_gen


def get_test_edges(paths: List[str], sep: str):
    # edges = set()
    edges = []
    for path in paths:
        with open(path, "r") as f:
            for line in f:
                tokens = line.strip().split(sep)
                etype = tokens[0]
                source = tokens[1]
                destination = tokens[2]
                label = tokens[3]
                edge = (etype, source, destination, label)
                # edges.add(edge)
                edges.append(edge)

    return edges


def main(args):
    data_path = f"{args.data_path}/{args.data_name}"
    attr_graph, context_gen = get_graph(data_path, args.false_edge_gen)
    attr_graph.dump_stats()

    stime = time.time()
    id_maps_dir = data_path
    ent2id = get_id_map(f"{id_maps_dir}/ent2id.txt")
    rel2id = get_id_map(f"{id_maps_dir}/rel2id.txt")
    print(len(ent2id), len(rel2id))

    # Load pretrained embedding from CompGCN
    if args.pretrained_method == "compgcn":
        pretrained_node_embedding = load_pickle(args.pretrained_embeddings)
    elif args.pretrained_method == "node2vec":
        if not os.path.exists(args.pretrained_embeddings):
            print("Run Node2vec to obtain pre-trained node embeddings ...")
            nx_G = attr_graph.G
            for edge in nx_G.edges():
                nx_G[edge[0]][edge[1]]["weight"] = 1
            nx_G = nx_G.to_undirected()

            G = Graph(nx_G, False, 1, 1)
            G.preprocess_transition_probs()
            walks = G.simulate_walks(10, 80)
            walks = [list(map(str, walk)) for walk in walks]
            model = Word2Vec(
                walks,
                size=args.base_embedding_dim,
                window=10,
                min_count=0,
                sg=1,
                workers=8,
                iter=1,
            )
            model.wv.save_word2vec_format(args.pretrained_embeddings)

        pretrained_node_embedding = load_pretrained_node2vec(
            args.pretrained_embeddings, ent2id, args.base_embedding_dim
        )

    print(
        "No. of nodes with pretrained embedding: ",
        len(pretrained_node_embedding),
    )

    valid_path = data_path + "/valid.txt"
    valid_edges_paths = [valid_path]
    valid_edges = list(get_test_edges(valid_edges_paths, " "))
    test_path = data_path + "/test.txt"
    test_edges_paths = [test_path]
    test_edges = list(get_test_edges(test_edges_paths, " "))
    print("No. edges in test data: ", len(test_edges))

    if args.is_pre_trained:
        link_prediction_eval(
            valid_edges, test_edges, ent2id, rel2id, pretrained_node_embedding
        )
    else:
        print("No pretrained embedding, no need to evaluate workflow 1.\n")

    print("***************PRETRAINING***************")
    pre_num_batches = setup_pretraining_input(args, attr_graph, context_gen, data_path)
    print("\n Run model for pre-training ...")
    # Masked nodes prediction
    pred_data, true_data = run_pretraining(
        args, attr_graph, pre_num_batches, pretrained_node_embedding, ent2id, rel2id
    )
    print("\n Begin evaluation for node prediction...")
    # accu, mse = run_evaluation(pred_data, true_data)

    # Link prediction
    ft_num_batches = setup_finetuning_input(args, attr_graph, context_gen)
    pred_data, true_data = run_finetuning_wkfl2(
        args, attr_graph, ft_num_batches, pretrained_node_embedding, ent2id, rel2id
    )
    print("\n Begin evaluation for link prediction...")
    valid_true_data = np.array(true_data["valid"])
    threshold = find_optimal_cutoff(valid_true_data, pred_data["valid"])[0]
    run_evaluation_main(
        test_edges, pred_data["test"], true_data["test"], threshold, header="workflow2"
    )
    # evaluate_all_epochs(args, attr_graph, ft_num_batches,
    #                    pretrained_node_embedding, ent2id, rel2id, test_edges)

    # WORKFLOW_3
    print("***************FINETUNING***************")
    print("\n Run model for finetuning ...")
    (
        pred_data_test,
        true_data_test,
        pred_data_valid,
        true_data_valid,
    ) = run_finetuning_wkfl3(
        args, attr_graph, ft_num_batches, pretrained_node_embedding, ent2id, rel2id
    )
    print("\n Begin evaluation for link prediction...")
    valid_true_data = np.array(true_data_valid)
    threshold = find_optimal_cutoff(valid_true_data, pred_data_valid)[0]
    # save the threshold values for later use
    json.dump(threshold, open(args.outdir + "/thresholds.json", "w"))
    run_evaluation_main(
        test_edges, pred_data_test, true_data_test, threshold, header="workflow3"
    )

    # evaluate after context inference

    etime = time.time()
    elapsed = etime - stime
    print(f"running time(seconds) on {args.data_name} data: {elapsed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_name", default="amazon_s", help="name of the dataset")
    parser.add_argument("--data_path", default="data", help="path to dataset")
    parser.add_argument("--outdir", default="output/default", help="path to output dir")
    parser.add_argument(
        "--pretrained_embeddings", help="absolute path to pretrained embeddings"
    )
    parser.add_argument(
        "--pretrained_method", default="node2vec", help="compgcn|node2vec"
    )
    # Walks options
    parser.add_argument(
        "--beam_width",
        default=4,
        type=int,
        help="beam width used for generating random walks",
    )
    parser.add_argument(
        "--num_walks_per_node", default=1, type=int, help="walks per node"
    )
    parser.add_argument("--walk_type", default="dfs", help="walk type bfs/dfs")
    parser.add_argument("--max_length", default=6, type=int, help="max length of walks")
    parser.add_argument(
        "--n_pred", default=1, help="number of tokens masked to be predicted"
    )
    parser.add_argument(
        "--max_pred", default=1, help="max number of tokens masked to be predicted"
    )
    # Pretraining options
    parser.add_argument("--lr", default=0.0001, type=float, help="learning rate")
    parser.add_argument(
        "--n_epochs", default=10, type=int, help="number of epochs for training"
    )
    parser.add_argument(
        "--checkpoint", default=20, type=int, help="checkpoint for validation"
    )
    parser.add_argument(
        "--base_embedding_dim",
        default=200,
        type=int,
        help="dimension of base embedding",
    )
    parser.add_argument(
        "--batch_size",
        default=128,
        type=int,
        help="number of data sample in each batch",
    )
    parser.add_argument(
        "--emb_dir",
        default="data",
        type=str,
        help="Used to generate embeddings path if --pretrained_embeddings is not set",
    )
    parser.add_argument(
        "--get_bert_encoder_embeddings",
        default=False,
        help="indicate if need to get node vectors from BERT encoder output, save code "
             "commented out in src/pretraining.py",
    )
    # BERT Layer options
    parser.add_argument(
        "--n_layers", default=4, type=int, help="number of encoder layers in bert"
    )
    parser.add_argument(
        "--d_model", default=200, type=int, help="embedding size in bert"
    )
    parser.add_argument("--d_k", default=64, type=int, help="dimension of K(=Q), V")
    parser.add_argument("--d_v", default=64, type=int, help="dimension of K(=Q), V")
    parser.add_argument("--n_heads", default=4, type=int, help="number of head in bert")
    parser.add_argument(
        "--d_ff",
        default=200 * 4,
        type=int,
        help="4*d_model, FeedForward dimension in bert",
    )
    # GCN Layer options
    parser.add_argument(
        "--is_pre_trained",
        action="store_true",
        help="if there is pretrained node embeddings",
    )
    parser.add_argument(
        "--gcn_option",
        default="no_gcn",
        help="preprocess bert input once or alternate gcn and bert, preprocess|alternate|no_gcn",
    )
    parser.add_argument(
        "--num_gcn_layers", default=2, type=int, help="number of gcn layers before bert"
    )
    parser.add_argument(
        "--node_edge_composition_func",
        default="mult",
        help="options for node and edge compostion, sub|circ_conv|mult|no_rel",
    )

    # Finetuning options
    parser.add_argument("--ft_lr", default=0.001, type=float, help="learning rate")
    parser.add_argument(
        "--ft_batch_size",
        default=128,
        type=int,
        help="number of data sample in each batch",
    )
    parser.add_argument(
        "--ft_checkpoint", default=1000, type=int, help="checkpoint for validation"
    )
    parser.add_argument(
        "--ft_d_ff", default=512, type=int, help="feedforward dimension in finetuning"
    )
    parser.add_argument(
        "--ft_layer", default="ffn", help="options for finetune layer: linear|ffn"
    )
    parser.add_argument(
        "--ft_drop_rate", default=0.1, type=float, help="dropout rate in finetuning"
    )
    parser.add_argument(
        "--ft_input_option",
        default="last4_cat",
        help="which output layer from graphbert will be used for finetuning, last|last4_cat|last4_sum",
    )
    parser.add_argument(
        "--false_edge_gen",
        default="double",
        help="false edge generation pattern/double/basic",
    )
    parser.add_argument(
        "--ft_n_epochs", default=10, type=int, help="number of epochs for training"
    )
    parser.add_argument(
        "--path_option",
        default="shortest",
        help="fine tuning context generation: shortest/all/pattern/random",
    )
    args = parser.parse_args()

    # Default values
    args.pretrained_embeddings, args.base_embedding_dim = get_set_embeddings_details(
        args
    )
    args.d_model = args.base_embedding_dim
    args.d_ff = args.base_embedding_dim * 4

    print("Args ", str(args))
    main(args)
