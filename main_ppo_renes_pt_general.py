import os

os.environ["CUDA_VISIBLE_DEVICES"] = "1"
import time
from collections import deque

import numpy as np

from a2c_ppo_acktr.algo.ppo_general import PPO

# from a2c_ppo_acktr.algo import gail
from a2c_ppo_acktr.arguments import get_args
from game_envs.envs_general import make_vec_envs as game_make_vec_envs
from a2c_ppo_acktr.model import Policy
from a2c_ppo_acktr.storage_general import RolloutStorage
from game2graph.game_data import gen_game_datasets
from configs import get_parser
from copy import deepcopy

# import numpy as np
import torch

from a2c_ppo_acktr import utils

# from a2c_ppo_acktr.envs import make_vec_envs
from game2graph.game_gnn_model import ResponseGraphEncoder
import csv


def process_obs_to_input(obs, graph_encoder, device):
    gnn_original = []
    gnn_current = []
    for p_obs in obs:
        gnn_original.append(p_obs["gnn"][0].to(device))
        gnn_current.append(p_obs["gnn"][1].to(device))
    original_out = graph_encoder(gnn_original)
    current_out = graph_encoder(gnn_current)
    ac_input = torch.concat([original_out, current_out], dim=-1)
    return ac_input


def game_evaluate(
    actor_critic, graph_encoder, game_dataset, game_args, num_processes, device
):
    eval_envs = game_make_vec_envs(
        game_dataset=game_dataset,
        args=game_args,
        num_processes=num_processes,
        device=device,
        is_train=False,
    )

    obs = eval_envs.reset()
    eval_masks = torch.zeros(num_processes, 1, device=device)
    eval_recurrent_hidden_states = torch.zeros(
        num_processes, actor_critic.recurrent_hidden_state_size, device=device
    )
    eval_episode_rewards = []
    eval_accumulate_rewards = []
    eval_episode_abs_rewards = []
    eval_accumulate_abs_rewards = []
    eval_episode_init_nc = []
    eval_episode_min_nc = []
    episodes = 0
    while True:
        with torch.no_grad():
            ac_input = process_obs_to_input(obs, graph_encoder, device)
            _, action, _, eval_recurrent_hidden_states = actor_critic.act(
                ac_input, eval_recurrent_hidden_states, eval_masks, deterministic=True
            )
            # print(action.shape)
            obs, _, done, infos = eval_envs.step(action)
            eval_masks = torch.tensor(
                [[0.0] if done_ else [1.0] for done_ in done],
                dtype=torch.float32,
                device=device,
            )

            flag = False
            for info in infos:
                if "episode" in info.keys():
                    # print(info["episode"]["r"])
                    # print(info["episode"]["abs_r"])
                    eval_episode_rewards.append(info["episode"]["r"])
                    eval_episode_abs_rewards.append(info["episode"]["abs_r"])
                    eval_episode_init_nc.append(info["episode"]["init_nc"])
                    eval_episode_min_nc.append(info["episode"]["min_nc"])
                    episodes += 1
                    if episodes % 1000 == 0:
                        print("{} episodes have been evaluated".format(episodes))
                if "eval" in info.keys():
                    flag = True
                    # print(info["eval"])
                    # print(info["abs_eval"])
                    eval_accumulate_rewards.append(info["eval"])
                    eval_accumulate_abs_rewards.append(info["abs_eval"])
            if flag:
                break

    eval_envs.close()
    print(
        "Evaluation using {} episodes: mean reward {:.5f}, eval: {}, abs mean reward: {}, abs eval: {}\n".format(
            episodes,
            np.mean(eval_episode_rewards),
            np.mean(eval_accumulate_rewards),
            np.mean(eval_episode_abs_rewards),
            np.mean(eval_accumulate_abs_rewards),
        )
    )
    # print(game_dataset[0])
    return {
        "rel_mean": np.mean(eval_episode_rewards),
        "rel_std": np.std(eval_episode_rewards),
        "abs_mean": np.mean(eval_episode_abs_rewards),
        "abs_std": np.std(eval_episode_abs_rewards),
        "init_nc_mean": np.mean(eval_episode_init_nc),
        "init_nc_std": np.std(eval_episode_init_nc),
        "min_nc_mean": np.mean(eval_episode_min_nc),
        "min_nc_std": np.std(eval_episode_min_nc),
    }


def main():
    args = get_args()
    # args.seed = 1000
    # args.seed = 12345
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    if args.cuda and torch.cuda.is_available() and args.cuda_deterministic:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    # print(device)

    # generate the game dataset
    # game_parser = get_parser()
    # game_args = game_parser.parse_args()
    game_args = args
    game_args.min_players = 2
    game_args.max_players = 3
    game_args.max_actions = 4
    game_args.min_actions = 2
    game_args.train_number = 3000
    game_args.test_number = 500
    game_args.max_steps = 50
    # game_args.meta_solver = args.meta_solver

    result_dir = "results"
    args.log_dir = "general"
    log_dir = os.path.join(result_dir, args.log_dir)
    eval_log_dir = log_dir + "_eval"
    utils.cleanup_log_dir(log_dir)
    utils.cleanup_log_dir(eval_log_dir)

    args.save_dir = log_dir

    experiment_str = (
        "seed_{}_step_{}_ms_{}_minp{}_maxp_{}_mina_{}_maxa_{}_tn_{}".format(
            args.seed,
            game_args.max_steps,
            game_args.meta_solver,
            game_args.min_players,
            game_args.max_players,
            game_args.min_actions,
            game_args.max_actions,
            game_args.train_number,
        )
    )
    filename = eval_log_dir + "/result_" + experiment_str + ".csv"
    # filename = "run_nfsp.sh"

    # f = open(file=filename, mode="w")

    eval_keys = [
        "rel_mean",
        "rel_std",
        "abs_mean",
        "abs_std",
        "init_nc_mean",
        "init_nc_std",
        "min_nc_mean",
        "min_nc_std",
    ]

    csv_writer = csv.writer(open(filename, "w", 1))
    csv_header = (
        [
            "num_update",
            "num_steps",
        ]
        + ["train_" + eval_key for eval_key in eval_keys]
        + ["test_" + eval_key for eval_key in eval_keys]
    )
    csv_writer.writerow(csv_header)

    train_dataset, test_dataset = gen_game_datasets(game_args)
    # print(train_dataset[0])
    # test_dataset = train_dataset
    envs = game_make_vec_envs(
        game_dataset=train_dataset,
        args=game_args,
        num_processes=args.num_processes,
        device=device,
    )
    # print(envs.observation_space.shape)

    graph_encoder = ResponseGraphEncoder(
        node_feature_dim=1,
        node_output_size=game_args.node_output_size,
        num_layers=game_args.gnn_layers,
        batch_norm=False,
    )
    graph_encoder.to(device)

    actor_critic = Policy(
        obs_shape=[2 * game_args.node_output_size],
        action_space=envs.action_space,
        base_kwargs={"recurrent": args.recurrent_policy},
    )
    actor_critic.to(device)

    agent = PPO(
        actor_critic,
        args.clip_param,
        args.ppo_epoch,
        args.num_mini_batch,
        args.value_loss_coef,
        args.entropy_coef,
        lr=args.lr,
        eps=args.eps,
        max_grad_norm=args.max_grad_norm,
        graph_encoder=graph_encoder,
    )

    obs = envs.reset()
    # print(obs)
    rollouts = RolloutStorage(
        args.num_steps,
        args.num_processes,
        envs.observation_space.shape,
        envs.action_space,
        actor_critic.recurrent_hidden_state_size,
    )
    rollouts.obs_general.append(deepcopy(obs))
    rollouts.to(device)

    start = time.time()
    num_updates = int(args.num_env_steps) // args.num_steps // args.num_processes
    episode_rewards = deque(maxlen=100)
    # fix_obs = torch.rand([args.num_processes, *envs.observation_space.shape]).to(device)
    for j in range(num_updates):
        if args.eval_interval is not None and j % args.eval_interval == 0:
            print("Starting the evaluation")
            actor_critic.eval()
            graph_encoder.eval()
            total_num_steps = j * args.num_processes * args.num_steps
            log_data = [
                j,
                total_num_steps,
            ]
            results = game_evaluate(
                actor_critic=actor_critic,
                graph_encoder=graph_encoder,
                game_dataset=train_dataset,
                game_args=game_args,
                num_processes=args.num_processes,
                device=device,
            )
            for eval_key in eval_keys:
                log_data.append(results[eval_key])

            results = game_evaluate(
                actor_critic=actor_critic,
                graph_encoder=graph_encoder,
                game_dataset=test_dataset,
                game_args=game_args,
                num_processes=args.num_processes,
                device=device,
            )

            for eval_key in eval_keys:
                log_data.append(results[eval_key])

            csv_writer.writerow(log_data)

        if args.use_linear_lr_decay:
            # decrease learning rate linearly
            utils.update_linear_schedule(
                agent.optimizer,
                j,
                num_updates,
                agent.optimizer.lr if args.algo == "acktr" else args.lr,
            )
        actor_critic.train()
        graph_encoder.train()
        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                # gnn_original = []
                # gnn_current = []
                # for p_obs in rollouts.obs_general[step]:
                #     gnn_original.append(p_obs['gnn'][0].to(device))
                #     gnn_current.append(p_obs['gnn'][1].to(device))
                # original_out = graph_encoder(gnn_original)
                # current_out = graph_encoder(gnn_current)
                # ac_input = torch.concat([original_out, current_out], dim=-1)
                ac_input = process_obs_to_input(
                    rollouts.obs_general[step], graph_encoder, device
                )
                (
                    value,
                    action,
                    action_log_prob,
                    recurrent_hidden_states,
                ) = actor_critic.act(
                    ac_input,
                    rollouts.recurrent_hidden_states[step],
                    rollouts.masks[step],
                )
            # print(action.shape)
            # Obser reward and next obs
            obs, reward, done, infos = envs.step(action)

            # print(obs.shape)
            for info in infos:
                if "episode" in info.keys():
                    # print(info["episode"]["r"])
                    episode_rewards.append(info["episode"]["r"])

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
            bad_masks = torch.FloatTensor(
                [[0.0] if "bad_transition" in info.keys() else [1.0] for info in infos]
            )
            rollouts.insert(
                obs,
                recurrent_hidden_states,
                action,
                action_log_prob,
                value,
                reward,
                masks,
                bad_masks,
            )

        with torch.no_grad():
            ac_input = process_obs_to_input(
                rollouts.obs_general[-1], graph_encoder, device
            )
            next_value = actor_critic.get_value(
                ac_input,
                rollouts.recurrent_hidden_states[-1],
                rollouts.masks[-1],
            ).detach()

        rollouts.compute_returns(
            next_value,
            args.use_gae,
            args.gamma,
            args.gae_lambda,
            args.use_proper_time_limits,
        )

        value_loss, action_loss, dist_entropy = agent.update(rollouts)
        rollouts.after_update()

        if (
            j % args.save_interval == 0 or j == num_updates - 1
        ) and args.save_dir != "":
            save_path = os.path.join(args.save_dir, args.algo)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            torch.save(
                [
                    actor_critic,
                    graph_encoder,
                    getattr(utils.get_vec_normalize(envs), "obs_rms", None),
                ],
                os.path.join(save_path, experiment_str + ".pt"),
            )

        # args.log_interval = 1
        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            total_num_steps = (j + 1) * args.num_processes * args.num_steps
            end = time.time()
            print(
                "Updates {}, num timesteps {}, FPS {} \n Last {} training episodes: "
                "mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}\n".format(
                    j,
                    total_num_steps,
                    int(total_num_steps / (end - start)),
                    len(episode_rewards),
                    np.mean(episode_rewards),
                    np.median(episode_rewards),
                    np.min(episode_rewards),
                    np.max(episode_rewards),
                    dist_entropy,
                    value_loss,
                    action_loss,
                )
            )
    # f.close()


if __name__ == "__main__":
    main()
