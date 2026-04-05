#!/usr/bin/env python3
import os, sys, time, json, logging
import requests
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
RPC_URL = os.getenv("RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
COLD_WALLET = os.getenv("COLD_WALLET")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")

if not all([RPC_URL, PRIVATE_KEY, COLD_WALLET, CONTRACT_ADDRESS]):
    print("Missing env variables")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    print("RPC not connected")
    sys.exit(1)

account = w3.eth.account.from_key(PRIVATE_KEY)
MY_WALLET = account.address

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# ===== CONFIGURATION =====
SIMULATION_MODE = True   # Поменяй на False, когда проверишь логи
HEALTH_FACTOR_THRESHOLD = 1.05
MIN_PROFIT_USD = 1.0
SCAN_INTERVAL = 15
AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
GRAPH_URL = "https://api.thegraph.com/subgraphs/name/aave/aave-v3-polygon"

# ===== ABI =====
LIQUIDATOR_ABI = [{"name":"liquidate","type":"function","inputs":[{"name":"user","type":"address"},{"name":"debtAsset","type":"address"},{"name":"collateralAsset","type":"address"},{"name":"debtAmount","type":"uint256"}],"outputs":[],"stateMutability":"nonpayable"}]
POOL_ABI = [{"name":"getUserAccountData","type":"function","inputs":[{"name":"user","type":"address"}],"outputs":[{"name":"","type":"uint256"},{"name":"totalDebtBase","type":"uint256"},{"name":"","type":"uint256"},{"name":"","type":"uint256"},{"name":"","type":"uint256"},{"name":"healthFactor","type":"uint256"}],"stateMutability":"view"}]
ERC20_ABI = [{"name":"decimals","type":"function","inputs":[],"outputs":[{"type":"uint8"}]}]

liquidator = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=LIQUIDATOR_ABI)
pool = w3.eth.contract(address=Web3.to_checksum_address(AAVE_POOL), abi=POOL_ABI)

def get_unhealthy_positions():
    query = f'{{positions(where:{{healthFactor_lt:"{HEALTH_FACTOR_THRESHOLD}"}}){{id user{{id}} healthFactor collateralInUSD debtInUSD collateralAsset{{id}} debtAsset{{id}}}}}}'
    try:
        resp = requests.post(GRAPH_URL, json={'query': query}, timeout=10)
        return resp.json().get('data', {}).get('positions', [])
    except Exception as e:
        logger.error(f"Graph error: {e}")
        return []

def calculate_profit(pos):
    try:
        debt = float(pos['debtInUSD'])
        if debt <= 0: return 0.0
        return max(0.0, float(pos['collateralInUSD']) * 0.05 - debt * 0.0009)
    except:
        return 0.0

def verify_onchain_hf(user):
    try:
        hf = pool.functions.getUserAccountData(Web3.to_checksum_address(user)).call()[5] / 1e18
        logger.debug(f"On-chain HF for {user[:10]}... = {hf:.4f}")
        return hf < 1.0
    except:
        return True

def get_token_decimals(token_addr):
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        return token.functions.decimals().call()
    except:
        return 18

def execute_liquidation(user, debt_asset, collateral_asset, debt_amount_wei):
    if SIMULATION_MODE:
        logger.info(f"[SIM] Would liquidate {user[:10]}..., debtAmount={debt_amount_wei} wei")
        return True, "SIM"
    try:
        nonce = w3.eth.get_transaction_count(MY_WALLET)
        gas_price = min(int(w3.eth.gas_price * 1.2), 500 * 10**9)
        tx = liquidator.functions.liquidate(
            Web3.to_checksum_address(user),
            Web3.to_checksum_address(debt_asset),
            Web3.to_checksum_address(collateral_asset),
            debt_amount_wei
        ).build_transaction({
            'from': MY_WALLET,
            'gas': 1_000_000,
            'gasPrice': gas_price,
            'nonce': nonce,
            'chainId': w3.eth.chain_id
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt['status'] == 1:
            logger.info(f"✅ Liquidation successful! Tx: {tx_hash.hex()}")
            return True, tx_hash.hex()
        else:
            logger.error(f"❌ Liquidation failed (revert). Tx: {tx_hash.hex()}")
            return False, tx_hash.hex()
    except Exception as e:
        logger.error(f"Execution error: {e}")
        return False, None

def main():
    logger.info("=== Aave V3 Polygon Liquidation Bot (Full) ===")
    logger.info(f"Contract: {CONTRACT_ADDRESS}")
    logger.info(f"Mode: {'SIMULATION' if SIMULATION_MODE else 'LIVE'}")
    scan = 0
    while True:
        scan += 1
        logger.info(f"--- Scan #{scan} ---")
        positions = get_unhealthy_positions()
        if not positions:
            logger.info("No unhealthy positions")
            time.sleep(SCAN_INTERVAL)
            continue
        logger.info(f"Found {len(positions)} positions with HF < {HEALTH_FACTOR_THRESHOLD}")
        for pos in positions:
            try:
                user = pos['user']['id']
                profit = calculate_profit(pos)
                if profit < MIN_PROFIT_USD:
                    continue
                hf = float(pos['healthFactor'])
                logger.info(f"Target: {user[:15]}... HF={hf:.4f} profit=${profit:.2f}")
                if not verify_onchain_hf(user):
                    logger.info("Position already healthy, skip")
                    continue
                debt_asset = pos.get('debtAsset', {}).get('id')
                collateral_asset = pos.get('collateralAsset', {}).get('id')
                if not debt_asset or not collateral_asset:
                    logger.warning("Missing asset addresses")
                    continue
                decimals = get_token_decimals(debt_asset)
                debt_amount_wei = int(float(pos['debtInUSD']) * (10**decimals))
                logger.info(f"Debt amount in wei: {debt_amount_wei} (decimals={decimals})")
                success, tx_info = execute_liquidation(user, debt_asset, collateral_asset, debt_amount_wei)
                if success and not SIMULATION_MODE:
                    logger.info("Profit should be sent to cold wallet by contract")
            except Exception as e:
                logger.error(f"Error processing position: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
        sys.exit(0)
