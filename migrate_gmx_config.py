"""Migrate only public configuration; preserve secrets and historical data.

Run with --apply to update .env. It never enables a signer or submits a trade.
"""
import argparse
from pathlib import Path

VALUES = {
    'EXCHANGE_ID': 'gmx', 'MARKET_DATA_EXCHANGE_ID': '', 'SYMBOL': 'BTC/USD [BTC-USDC]',
    'GMX_CHAIN_ID': '42161', 'GMX_LIVE_ENABLED': 'false', 'DRY_RUN': 'true',
    'USE_TESTNET': 'false', 'ENTRY_POST_ONLY': 'false', 'GEMINI_MODEL': 'gemini-3.8-flash',
    'MAKER_FEE': '0.0007', 'TAKER_FEE': '0.0007', 'DATA_DIR': './data/gmx',
    'GMX_EXECUTION_BUDGET_USD': '3.00', 'GMX_CARRY_BUDGET_PCT': '0.001',
    'GMX_MIN_NET_REWARD_USD': '0.25', 'GMX_MAX_PRICE_AGE_SEC': '90', 'GMX_MAX_HOLDING_MINUTES': '60',
}


def migrate(path):
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    seen = set()
    output = []
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
        if key in VALUES:
            if key not in seen:
                output.append(f'{key}={VALUES[key]}')
            seen.add(key)
        else:
            output.append(line)
    output += [f'{key}={value}' for key, value in VALUES.items() if key not in seen]
    for key in ('GMX_WALLET_ADDRESS', 'GMX_PRIVATE_KEY'):
        if not any(line.startswith(key + '=') for line in output):
            output.append(key + '=')
    path.write_text('\n'.join(output) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        migrate(Path(__file__).with_name('.env'))
        print('GMX paper configuration applied. Existing secrets and historical data preserved. Signer disabled.')
    else:
        print('Use --apply to migrate .env to GMX paper mode. No changes made.')
