import logging
import sys
import os
import pickle
import requests
from datetime import datetime, timedelta

import forta_agent
import pandas as pd
from forta_agent import get_json_rpc_url
from hexbytes import HexBytes
from web3 import Web3

from constants import (ENTITY_CLUSTERS_MAX_QUEUE_SIZE, ALERT_MAX_QUEUE_SIZE, FP_ALERTS_QUEUE_MAX_SIZE, BASE_BOTS, ENTITY_CLUSTER_BOT_ALERT_ID,
                       ALERTED_CLUSTERS_MAX_QUEUE_SIZE, FP_MITIGATION_BOTS, ALERTS_LOOKBACK_WINDOW_IN_HOURS, ENTITY_CLUSTER_BOT,
                       FP_MITIGATION_BOTS_DATE_LOOKBACK_WINDOW_IN_HOURS, ENTITY_CLUSTER_BOT_DATE_LOOKBACK_WINDOW_IN_HOURS,
                       ALERTS_KEY, ALERTED_CLUSTERS_KEY, ENTITY_CLUSTER_ALERTS_KEY, FP_MITIGATION_ALERTS_KEY)
from luabase import Luabase


web3 = Web3(Web3.HTTPProvider(get_json_rpc_url()))
luabase = Luabase()

DATABASE = "https://research.forta.network/database/bot/"

FINDINGS_CACHE = []
ENTITY_CLUSTER_ALERTS = []
ALERTS = []
ALERTED_CLUSTERS = []
FP_MITIGATION_ALERTS = []
MUTEX = False
ICE_PHISHING_MAPPINGS_DF = pd.DataFrame()

root = logging.getLogger()
root.setLevel(logging.DEBUG)  # TODO - change to INFO

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)  # TODO - change to INFO
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)


def initialize():
    """
    this function initializes the state variables that are tracked across tx and blocks
    it is called from test to reset state between tests
    """
    print('initializing')
    global ALERTED_CLUSTERS
    alerted_clusters = load(ALERTED_CLUSTERS_KEY)
    ALERTED_CLUSTERS = [] if alerted_clusters is None else list(alerted_clusters)

    global ALERTS
    alerts = load(ALERTS_KEY)
    ALERTS = [] if alerts is None else list(alerts)

    global ENTITY_CLUSTER_ALERTS
    entity_cluster_alerts = load(ENTITY_CLUSTER_ALERTS_KEY)
    ENTITY_CLUSTER_ALERTS = [] if entity_cluster_alerts is None else list(entity_cluster_alerts)

    global FP_MITIGATION_ALERTS
    fp_mitigation_alerts = load(FP_MITIGATION_ALERTS_KEY)
    FP_MITIGATION_ALERTS = [] if fp_mitigation_alerts is None else list(fp_mitigation_alerts)

    global FINDINGS_CACHE
    FINDINGS_CACHE = []

    global MUTEX
    MUTEX = False

    subscription_json = []
    for bot, alertId, stage, ad_scorer in BASE_BOTS:
        subscription_json.append({"botId": bot, "alertId": alertId})

    for bot, alertId in FP_MITIGATION_BOTS:
        subscription_json.append({"botId": bot, "alertId": alertId})

    subscription_json.append({"botId": ENTITY_CLUSTER_BOT, "alertId": ENTITY_CLUSTER_BOT_ALERT_ID})

    return {"alertConfig": {"subscriptions": [subscription_json]}}


def is_contract(w3, addresses) -> bool:
    """
    this function determines whether address/ addresses is a contract; if all are contracts, returns true; otherwise false
    :return: is_contract: bool
    """
    if addresses is None:
        return True

    is_contract = True
    for address in addresses.split(','):
        try:
            code = w3.eth.get_code(Web3.toChecksumAddress(address))
        except Exception as e:
            logging.error(f"Exception in is_contract {e}")

        is_contract = is_contract & (code != HexBytes('0x'))

    return is_contract


def is_address(w3, addresses: str) -> bool:
    """
    this function determines whether address is a valid address
    :return: is_address: bool
    """
    if addresses is None:
        return True

    is_address = True
    for address in addresses.split(','):
        for c in ['a', 'b', 'c', 'd', 'e', 'f', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9']:
            test_str = c + c + c + c + c + c + c + c + c  # make a string of length 9; I know this is ugly, but regex didnt work
            if test_str in address.lower():
                is_address = False

    return is_address


def replace_with_cluster_identifiers(addresses: list, clusters: list) -> list:
    cluster_identifiers = []

    for address in addresses:
        address_lower = address.lower()
        found = False
        for cluster in clusters:
            cluster_lower = cluster.lower()
            if address_lower in cluster_lower:
                found = True
                cluster_identifiers.append(cluster_lower)
                break
        if not found:
            cluster_identifiers.append(address_lower)

    return cluster_identifiers


def swap_addresses_with_clusters(addresses: list, df_address_clusters_exploded: pd.DataFrame) -> list:
    df_addresses = pd.DataFrame(addresses, columns=["addresses"])
    df_addresses["addresses"] = df_addresses["addresses"].apply(lambda x: x.lower())

    df_addresses_joined = df_addresses.join(df_address_clusters_exploded, on="addresses", how="left", lsuffix="_alert", rsuffix="_cluster")
    df_addresses_joined = df_addresses_joined.reset_index()
    if len(df_addresses_joined) > 0:
        df_addresses_joined["cluster_identifiers"] = df_addresses_joined.apply(lambda x: x["addresses"] if pd.isnull(x["entity_addresses"]) else x["entity_addresses"], axis=1)
        df_addresses_joined.drop(columns=["entity_addresses_arr", "entity_addresses"], inplace=True)

        return df_addresses_joined["cluster_identifiers"].tolist()
    else:
        return []


def detect_attack(w3, luabase: Luabase, alert_event: forta_agent.alert_event.AlertEvent):
    """
    this function returns finding for any address with at least 3 alerts observed on that address; it will generate an anomaly score
    :return: findings: list
    """
    logging.debug(f"detect attack called for alert_hash {alert_event.alert_hash}")
    global ALERTED_CLUSTERS
    global ALERTS
    global FP_MITIGATION_ALERTS
    global ENTITY_CLUSTER_ALERTS
    global MUTEX

    # categorize incoming alert and put into appropriate list based on lookback window and queue size
    update_list(alert_event, ALERT_MAX_QUEUE_SIZE, BASE_BOTS, ALERTS_LOOKBACK_WINDOW_IN_HOURS, ALERTS)
    update_list(alert_event, FP_ALERTS_QUEUE_MAX_SIZE, FP_MITIGATION_BOTS, FP_MITIGATION_BOTS_DATE_LOOKBACK_WINDOW_IN_HOURS, FP_MITIGATION_ALERTS)
    update_list(alert_event, ENTITY_CLUSTERS_MAX_QUEUE_SIZE, [(ENTITY_CLUSTER_BOT, ENTITY_CLUSTER_BOT_ALERT_ID)], ENTITY_CLUSTER_BOT_DATE_LOOKBACK_WINDOW_IN_HOURS, ENTITY_CLUSTER_ALERTS)

    #  assess whether we generate a finding
    #  note, only one instance will be running at a time to keep up with alert volume
    if not MUTEX:
        MUTEX = True

        #  update alert counts for 48 hour period using luabase

        MUTEX = False


def update_alerted_clusters(cluster: str):
    """
    this function maintains a list clusters; holds up to CLUSTER_QUEUE_SIZE in memory
    :return: None
    """
    global ALERTED_CLUSTERS

    ALERTED_CLUSTERS.append(cluster.lower())
    if len(ALERTED_CLUSTERS) > ALERTED_CLUSTERS_MAX_QUEUE_SIZE:
        ALERTED_CLUSTERS.pop(0)


def in_list(alert_event: forta_agent.alert_event.AlertEvent, bots: tuple) -> bool:
    """
    this function returns True if the alert is from a bot in the bots tuple
    :return: bool
    """
    for tup in bots:
        if alert_event.alert.source.bot['id'] == tup[0] and alert_event.alert.alert_id == tup[1]:
            return True

    return False


def update_list(new_alert_event: forta_agent.alert_event.AlertEvent, max_queue_size: int, bots: tuple, lookback_window_in_hours: int, alert_list: list):
    """
    this function maintains list; holds up to MAX_QUEUE_SIZE in memory; it removes alerts that are older than ALERTS_LOOKBACK_WINDOW_IN_HOURS
    :return: None
    """
    if in_list(new_alert_event, bots):
        alert_list.append(new_alert_event.alert)

    cutoff_date = datetime.now() - timedelta(hours=lookback_window_in_hours)
    for alert in alert_list:
        # 2022-11-19T16:20:25232199Z
        if datetime.strptime(alert.created_at, "%Y-%m-%dT%H:%M:%S%fZ") < cutoff_date:
            alert_list.remove(alert)

    if len(alert_list) > max_queue_size:
        removed_alert = alert_list.pop(0)
        logging.warn(f"removed alert from list: {removed_alert.hash}")


def update_entity_clusters(clusters: str):
    """
    this function maintains a list clusters; holds up to CLUSTER_QUEUE_SIZE in memory
    :return: None
    """
    global ENTITY_CLUSTERS

    ENTITY_CLUSTERS.append(clusters)
    if len(ENTITY_CLUSTERS) > ENTITY_CLUSTERS_MAX_QUEUE_SIZE:
        ENTITY_CLUSTERS.pop(0)


def persist_state():
    global ALERTS_KEY
    global FP_MITIGATION_ALERTS_KEY
    global ALERTED_CLUSTERS_KEY
    global ENTITY_CLUSTER_ALERTS_KEY

    persist(ALERTS_KEY, ALERTS_KEY)
    persist(FP_MITIGATION_ALERTS_KEY, FP_MITIGATION_ALERTS_KEY)
    persist(ENTITY_CLUSTER_ALERTS_KEY, ENTITY_CLUSTER_ALERTS_KEY)
    persist(ALERTED_CLUSTERS_KEY, ALERTED_CLUSTERS_KEY)
    logging.info("Persisted bot state.")


def persist(obj: object, key: str):
    if os.environ.get('LOCAL_NODE') is None:
        logging.info(f"Persisting {key} using API")
        bytes = pickle.dumps(obj)
        token = forta_agent.fetch_jwt({})

        headers = {"Authorization": f"Bearer {token}"}
        res = requests.post(f"{DATABASE}{key}", data=bytes, headers=headers)
        logging.info(f"Persisting {key} to database. Response: {res}")
        return
    else:
        logging.info(f"Persisting {key} locally")
        pickle.dump(obj, open(key, "wb"))


def load(key: str) -> object:
    if os.environ.get('LOCAL_NODE') is None:
        logging.info(f"Loading {key} using API")
        token = forta_agent.fetch_jwt({})
        logging.info("Fetched token")
        logging.info(token)
        headers = {"Authorization": f"Bearer {token}"}
        res = requests.get(f"{DATABASE}{key}", headers=headers)
        logging.info(f"Loaded {key}. Response: {res}")
        if res.status_code == 200 and len(res.content) > 0:
            return pickle.loads(res.content)
        else:
            logging.info(f"{key} does not exist")
    else:
        # load locally
        logging.info(f"Loading {key} locally")
        if os.path.exists(key):
            return pickle.load(open(key, "rb"))
        else:
            logging.info(f"File {key} does not exist")
    return None


def provide_handle_alert(w3, luabase: Luabase):
    logging.debug("provide_handle_alert called")

    def handle_alert(alert_event: forta_agent.alert_event.AlertEvent) -> list:
        global FINDINGS_CACHE
        global MUTEX

        detect_attack(w3, luabase, alert_event)
        # if not MUTEX:
        #     thread = threading.Thread(target=detect_attack, args=(w3, luabase))
        #     thread.start()

        # uncomment for local testing of tx/block ranges (ok for npm run start); otherwise the process will exit
        # while (thread.is_alive()):
        #    pass
        findings = FINDINGS_CACHE
        FINDINGS_CACHE = []
        return findings

    return handle_alert


real_handle_alert = provide_handle_alert(web3, luabase)


def handle_alert(alert_event: forta_agent.alert_event.AlertEvent) -> list:
    logging.debug("handle_alert called")
    return real_handle_alert(alert_event)
