import configparser
cfg = configparser.ConfigParser()
cfg.read("config.ini")
HOST = cfg["server"]["host"]
PORT = int(cfg["server"]["port"])
WORKERS = int(cfg["server"]["workers"])
