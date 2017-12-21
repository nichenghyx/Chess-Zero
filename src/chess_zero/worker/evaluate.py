import os
from logging import getLogger
from time import sleep
import chess
from chess_zero.agent.model_chess import ChessModel
from chess_zero.agent.player_chess import ChessPlayer
from chess_zero.config import Config
from chess_zero.env.chess_env import ChessEnv, Winner
from chess_zero.lib import tf_util
from chess_zero.lib.data_helper import get_next_generation_model_dirs, prettyprint
from chess_zero.lib.model_helper import save_as_best_model, load_best_model_weight
from multiprocessing.pool import Pool
from multiprocessing import Manager

logger = getLogger(__name__)

def start(config: Config):
    #tf_util.set_session_config(config.play.vram_frac)
    return EvaluateWorker(config).start()

class EvaluateWorker:
    def __init__(self, config: Config):
        """
        :param config:
        """
        self.config = config
        self.play_config = config.eval.play_config
        self.current_model = self.load_current_model()
        self.m = Manager()
        self.current_pipes = self.m.list([self.current_model.get_pipes(self.play_config.search_threads) for _ in range(self.play_config.max_processes)])

    def start(self):
        while True:
            ng_model, model_dir = self.load_next_generation_model()
            logger.debug(f"start evaluate model {model_dir}")
            ng_is_great = self.evaluate_model(ng_model)
            # if ng_is_great:
            #     logger.debug(f"New Model become best model: {model_dir}")
            #     save_as_best_model(ng_model)
            #     self.current_model = ng_model
            #self.remove_model(model_dir)
            break

    def evaluate_model(self, ng_model):
        ng_pipes = self.m.list([ng_model.get_pipes(self.play_config.search_threads) for _ in range(self.play_config.max_processes)])

        futures = []
        pool = Pool(processes=3, initializer=setpipes, initargs=(self.current_pipes,ng_pipes))
        for game_idx in range(self.config.eval.game_num):
            futures.append(pool.apply_async(play_game, args=(self.config, game_idx % 2 == 0)))

        results = []
        win_rate = 0
        game_idx = 0
        for fut in futures:
            # ng_score := if ng_model win -> 1, lose -> 0, draw -> 0.5
            ng_score, env, current_white = fut.get()           
            results.append(ng_score)
            win_rate = sum(results) / len(results)
            logger.debug(f"game {game_idx:3}: ng_score={ng_score:.1f} as {'black' if current_white else 'white'} "
                         f"{'by resign ' if env.resigned else '          '}"
                         f"win_rate={win_rate*100:5.1f}% "
                         f"{env.board.fen()}")
            colors = ("current_model", "ng_model")
            if not current_white:
                colors=reversed(colors)

            prettyprint(env, colors)

            if results.count(0) >= self.config.eval.game_num * (1-self.config.eval.replace_rate):
                logger.debug(f"lose count reach {results.count(0)} so give up challenge")
                break
            if results.count(1) >= self.config.eval.game_num * self.config.eval.replace_rate:
                logger.debug(f"win count reach {results.count(1)} so change best model")
                break
            game_idx += 1

        win_rate = sum(results) / len(results)
        logger.debug(f"winning rate {win_rate*100:.1f}%")
        return win_rate >= self.config.eval.replace_rate

    def remove_model(self, model_dir):
        return 
        rc = self.config.resource
        config_path = os.path.join(model_dir, rc.next_generation_model_config_filename)
        weight_path = os.path.join(model_dir, rc.next_generation_model_weight_filename)
        os.remove(config_path)
        os.remove(weight_path)
        os.rmdir(model_dir)

    def load_current_model(self):
        model = ChessModel(self.config)
        load_best_model_weight(model)
        return model

    def load_next_generation_model(self):
        rc = self.config.resource
        while True:
            dirs = get_next_generation_model_dirs(self.config.resource)
            if dirs:
                break
            logger.info("There is no next generation model to evaluate")
            sleep(60)
        model_dir = dirs[-1] if self.config.eval.evaluate_latest_first else dirs[0]
        config_path = os.path.join(model_dir, rc.next_generation_model_config_filename)
        weight_path = os.path.join(model_dir, rc.next_generation_model_weight_filename)
        model = ChessModel(self.config)
        model.load(config_path, weight_path)
        return model, model_dir

def setpipes(cur, ng):
    global current_pipes
    current_pipes = cur.pop()
    global ng_pipes
    ng_pipes = ng.pop()

def play_game(config, current_white: bool) -> (float, ChessEnv):
    global current_pipes
    global ng_pipes
    env = ChessEnv().reset()

    current_player = ChessPlayer(config, pipes=current_pipes, play_config=config.eval.play_config)
    ng_player = ChessPlayer(config, pipes=ng_pipes, play_config=config.eval.play_config)
    if current_white:
        white, black = current_player, ng_player
    else:
        white, black = ng_player, current_player

    while not env.done:
        if env.board.turn == chess.WHITE:
            action = white.action(env)
        else:
            action = black.action(env)
        env.step(action)
        if env.num_halfmoves >= config.eval.max_game_length:
            env.adjudicate()

    if env.winner == Winner.draw:
        ng_score = 0.5
    elif env.whitewon == current_white:
        ng_score = 0
    else:
        ng_score = 1
    return ng_score, env, current_white