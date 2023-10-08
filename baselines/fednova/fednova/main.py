import pandas as pd
import torch
import hydra
from omegaconf import DictConfig, OmegaConf
from flwr.common import ndarrays_to_parameters
import flwr as fl
from hydra.utils import instantiate
import numpy as np
import random
import os
import time
from collections import OrderedDict
from functools import partial
from fednova.dataset import load_datasets
from fednova.client import gen_client_fn
from fednova.strategy import FedNova, weighted_average
from fednova.utils import fit_config
from fednova.models import test


@hydra.main(config_path="conf", config_name="base", version_base=None)
def main(cfg: DictConfig) -> None:
	"""Run the baseline.

    Parameters
    ----------
    cfg : DictConfig
        An omegaconf object that stores the hydra config.
    """
	start = time.time()

	# Set seeds for reproduceability
	torch.manual_seed(cfg.seed)
	np.random.seed(cfg.seed)
	random.seed(cfg.seed)
	if torch.cuda.is_available():
		torch.cuda.manual_seed(cfg.seed)
		# torch.backends.cudnn.deterministic = True

	# 1. Print parsed config
	print(OmegaConf.to_yaml(cfg))

	# 2. Prepare your dataset and directories

	if not os.path.exists(cfg.datapath):
		os.makedirs(cfg.datapath)
	if not os.path.exists(cfg.checkpoint_path):
		os.makedirs(cfg.checkpoint_path)

	trainloaders, testloader, data_ratios = load_datasets(cfg)
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

	if cfg.mode == "test":
		checkpoint = np.load(f"{cfg.checkpoint_path}bestModel_{cfg.exp_name}_varEpochs_{cfg.var_local_epochs}.npz", allow_pickle=True)
		model = instantiate(cfg.model)
		params_dict = zip(model.state_dict().keys(), checkpoint['arr_0'])
		state_dict = OrderedDict({k: torch.Tensor(v) for k, v in params_dict})
		model.load_state_dict(state_dict)
		loss, accuracy = test(model.to(device), testloader, device)
		print("----Loss: {}, Accuracy: {} on Test set ------".format(loss, accuracy))
		return None


	# 3. Define your clients

	client_fn = gen_client_fn(num_epochs=cfg.num_epochs,
							  trainloaders=trainloaders,
							  testloader=testloader,
							  data_ratios=data_ratios,
							  model=cfg.model,
							  exp_config=cfg)

	init_parameters = [layer_param.cpu().numpy() for _, layer_param in instantiate(cfg.model).state_dict().items()]
	init_parameters = ndarrays_to_parameters(init_parameters)

	eval_fn = partial(test, instantiate(cfg.model), testloader, device)

	# 4. Define your strategy
	strategy = FedNova(exp_config=cfg,
					   evaluate_metrics_aggregation_fn=weighted_average,
					   accept_failures=False,
					   on_evaluate_config_fn=fit_config,
					   initial_parameters=init_parameters,
					   evaluate_fn=eval_fn
					   )

	# 5. Start Simulation

	history = fl.simulation.start_simulation(client_fn=client_fn,
											 num_clients=cfg.num_clients,
											 config=fl.server.ServerConfig(num_rounds=cfg.num_rounds),
											 strategy=strategy,
											 client_resources=cfg.client_resources,
											 ray_init_args={"ignore_reinit_error": True, "num_cpus": 8})


	# 6. Save your results
	# save_path = HydraConfig.get().runtime.output_dir
	save_path = cfg.results_dir
	if not os.path.exists(save_path):
		os.makedirs(save_path)

	round, train_loss = zip(*history.losses_distributed)
	_, train_accuracy = zip(*history.metrics_distributed["accuracy"])
	_, test_loss = zip(*history.losses_centralized)
	_, test_accuracy = zip(*history.metrics_centralized["accuracy"])

	file_name = f"{save_path}fednova_varEpoch_{cfg.var_local_epochs}_lr_{cfg.optimizer.lr}_momentum_{cfg.optimizer.momentum}_gmf_{cfg.optimizer.gmf}_" \
				f"mu_{cfg.optimizer.mu}.csv"

	df = pd.DataFrame({"round": round, "train_loss": train_loss, "train_accuracy": train_accuracy,
					   "test_loss": test_loss, "test_accuracy": test_accuracy})

	df.to_csv(file_name, index=False)

	print("---------Experiment Completed in : {} minutes".format((time.time() - start) / 60))


if __name__ == "__main__":
	main()
