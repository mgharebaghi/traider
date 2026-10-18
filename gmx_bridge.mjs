// JSON-over-stdin boundary. Private keys never appear in arguments or output.
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { GmxApiSdk, PrivateKeySigner, parsePrepareOrderError } = require('@gmx-io/sdk/v2');
const stringify = (v) => JSON.stringify(v, (_, x) => typeof x === 'bigint' ? x.toString() : x);
const pack = (v) => JSON.stringify(v, (_, x) => typeof x === 'bigint' ? { $bigint: x.toString() } : x);
const unpack = (v) => JSON.parse(v, (_, x) => x && typeof x === 'object' && '$bigint' in x ? BigInt(x.$bigint) : x);
let operation = '';
try {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const p = input.trim() ? JSON.parse(input) : { op: process.argv[2] ?? 'markets' };
  operation = p.op;
  const chainId = Number(p.chainId ?? 42161);
  if (![42161, 43114].includes(chainId)) throw Error('Unsupported chain');
  const sdk = new GmxApiSdk({ chainId });
  let result;
  switch (p.op) {
    case 'markets': result = await sdk.fetchMarkets(); break;
    case 'tokens': result = await sdk.fetchTokens(); break;
    case 'ticker': {
      const [tickers, values] = await Promise.all([sdk.fetchMarketsTickers({ symbols: [p.symbol] }), sdk.fetchMarketsValues()]);
      result = { tickers, values }; break;
    }
    case 'candles': result = await sdk.fetchOhlcv({ symbol: p.symbol, timeframe: p.timeframe, limit: p.limit }); break;
    case 'account': {
      const [balances, positions, orders, allowances] = await Promise.all([
        sdk.fetchWalletBalances({ address: p.address }),
        sdk.fetchPositionsInfo({ address: p.address, includeRelatedOrders: true }),
        sdk.fetchOrders({ address: p.address }),
        sdk.fetchAllowances({ address: p.address, spender: 'router' }),
      ]);
      result = { balances, positions, orders, allowances }; break;
    }
    case 'prepare': {
      const r = p.request;
      for (const k of ['size', 'triggerPrice']) if (r[k] !== undefined) r[k] = BigInt(r[k]);
      if (r.collateralToPay) r.collateralToPay.amount = BigInt(r.collateralToPay.amount);
      for (const t of r.tpsl ?? []) {
        t.triggerPrice = BigInt(t.triggerPrice);
        if (t.size !== undefined) t.size = BigInt(t.size);
      }
      const prepared = await sdk.prepareOrder(r);
      const tokens = await sdk.fetchTokensData();
      const native = tokens.find(t => t.isNative);
      if (!native?.prices?.maxPrice) throw Error('Native fee price unavailable');
      const executionFeeUsd = prepared.estimates
        ? prepared.estimates.executionFeeAmount * native.prices.maxPrice / (10n ** BigInt(native.decimals)) : null;
      const payment = prepared.payload.gasPaymentParams;
      const paymentToken = payment && tokens.find(t => t.address.toLowerCase() === payment.gasPaymentTokenAddress.toLowerCase());
      const paymentFeeUsd = paymentToken ? BigInt(payment.gasPaymentTokenAmount) * paymentToken.prices.maxPrice / (10n ** BigInt(paymentToken.decimals)) : null;
      result = { prepared: pack(prepared), estimates: prepared.estimates,
        executionFeeUsd, paymentFeeUsd,
        requestId: prepared.requestId, warnings: [...(prepared.warnings ?? []), ...(prepared.validationWarnings ?? [])] };
      break;
    }
    case 'prepareCancel': {
      const prepared = await sdk.prepareCancelOrder({ orderIds: p.orderIds, from: p.address, mode: 'express' });
      result = { prepared: pack(prepared), requestId: prepared.requestId }; break;
    }
    case 'submit': {
      if (process.env.GMX_LIVE_ENABLED !== 'true') throw Error('Live trading disabled');
      const signer = new PrivateKeySigner(process.env.GMX_PRIVATE_KEY ?? '');
      if (signer.address.toLowerCase() !== p.address.toLowerCase()) throw Error('Signer/address mismatch');
      const prepared = unpack(p.prepared);
      if (prepared.mode !== 'express' || prepared.payloadType !== 'typed-data') throw Error('Unsupported payload');
      const signature = await sdk.signOrder(prepared, signer);
      result = await sdk.submitOrder({ mode: prepared.mode, requestId: prepared.requestId,
        signature, from: p.address, idempotencyKey: prepared.idempotencyKey,
        eip712Data: { batchParams: prepared.payload.batchParams, relayParams: prepared.payload.relayParams } });
      break;
    }
    case 'status': result = await sdk.fetchOrderStatus({ requestId: p.requestId }); break;
    case 'history': result = await sdk.fetchTrades({ address: p.address, marketAddress: p.marketAddress,
      since: p.since, limit: 100, cursor: p.cursor }); break;
    case 'tradeSample': {
      const history = await sdk.searchTrades({ forAllAccounts: true, limit: 1,
        marketsDirections: [{ marketAddress: p.marketAddress, direction: 'long' }],
        orderEventCombinations: [{ eventName: 'OrderExecuted', orderType: 4 }] });
      result = history.trades.map(t => Object.fromEntries(['executionPrice', 'pnlUsd', 'basePnlUsd', 'priceImpactUsd',
        'positionFeeAmount', 'borrowingFeeAmount', 'fundingFeeAmount', 'sizeDeltaUsd', 'totalImpactUsd',
        'priceImpactDiffUsd', 'proportionalPendingImpactUsd', 'swapFeeUsd', 'collateralTotalCostAmount',
        'traderDiscountAmount', 'liquidationFeeAmount', 'srcChainId', 'collateralTokenPriceMin'].map(k => [k, t[k]])));
      break;
    }
    default: throw Error('Unknown operation');
  }
  process.stdout.write(stringify({ ok: true, result }));
} catch (error) {
  // SDK errors may contain signed payloads or URLs: expose only a safe class name.
  const parsed = parsePrepareOrderError(error);
  const code = ['INVALID_PARAMS', 'MARKET_NOT_FOUND', 'TOKEN_NOT_FOUND', 'NO_SWAP_PATH', 'POSITION_NOT_FOUND', 'INSUFFICIENT_LIQUIDITY'].includes(parsed?.code) ? parsed.code : undefined;
  const fields = parsed?.message && typeof parsed.message === 'object' ? Object.keys(parsed.message) : undefined;
  const detail = operation === 'prepare' && error?.statusCode === 400
    ? String(error?.body?.message ?? error?.body?.error ?? '').replace(/0x[a-fA-F0-9]+/g, '[hex]').slice(0, 300) : undefined;
  process.stdout.write(JSON.stringify({ ok: false, error: error?.name ?? 'GMXError', statusCode: error?.statusCode, code, fields, detail }));
  process.exitCode = 1;
}
