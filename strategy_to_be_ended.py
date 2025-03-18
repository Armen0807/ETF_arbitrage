import yfinance as yf
import pandas as pd
import numpy as np
from statsmodels.api import OLS, add_constant
from arch import arch_model
from typing import Tuple, Dict


class StatisticalArbitrageStrategy:
    def __init__(self, params: Dict):
        self.ETF = params['ETF']
        self.Stock = params['Stock']
        self.C_total = params['C_total']
        self.N_yr = 252

        self.theta_entry = params['theta_entry']
        self.theta_exit = params['theta_exit']
        self.W_beta = params['W_beta']
        self.W_res = params['W_res']
        self.T_max = params['T_max']
        self.f_risk = params['f_risk']
        self.theta_vol = params['theta_vol']
        self.C_transaction = params['C_transaction']

        self.etf_data = None
        self.stock_data = None
        self.residuals = pd.Series()
        self.positions = []

    def download_data(self, start_date: str, end_date: str) -> None:
        """Download historical data"""
        try:
            self.etf_data = yf.download(self.ETF, start=start_date, end=end_date)['Close']
            self.stock_data = yf.download(self.Stock, start=start_date, end=end_date)['Close']
        except Exception as e:
            print(f"Error downloading data: {e}")

    def calculate_returns(self) -> Tuple[pd.Series, pd.Series]:
        """Calculate log returns"""
        etf_returns = np.log(self.etf_data).diff().dropna()
        stock_returns = np.log(self.stock_data).diff().dropna()
        return etf_returns, stock_returns

    def estimate_dynamic_beta(self, etf_returns: pd.Series, stock_returns: pd.Series) -> pd.Series:
        """Estimate dynamic beta using rolling window"""
        betas = pd.Series(index=etf_returns.index, dtype=float)

        for i in range(self.W_beta, len(etf_returns)):
            window = slice(i - self.W_beta, i)
            X = add_constant(etf_returns[window])
            model = OLS(stock_returns[window], X).fit()
            betas[etf_returns.index[i]] = model.params.iloc[1]  # Use .iloc for positional indexing

        return betas.dropna()

    def calculate_residuals(self, etf_returns: pd.Series, stock_returns: pd.Series) -> None:
        """Calculate standardized residuals using GARCH model"""
        betas = self.estimate_dynamic_beta(etf_returns, stock_returns)

        common_idx = betas.index.intersection(etf_returns.index).intersection(stock_returns.index)

        if len(common_idx) == 0:
            raise ValueError("No common indices found between betas, etf_returns, and stock_returns.")

        residuals = stock_returns.loc[common_idx] - betas.loc[common_idx] * etf_returns.loc[common_idx]

        if not isinstance(residuals, pd.Series):
            raise ValueError("Residuals must be a pandas Series.")

        residuals = residuals.dropna()

        if len(residuals) < 10:  # Minimum number of observations for GARCH
            raise ValueError("Not enough data points to fit GARCH model.")

        am = arch_model(residuals, vol='Garch', p=1, q=1)
        res = am.fit(disp='off')
        vol = res.conditional_volatility

        self.residuals = (residuals - residuals.rolling(self.W_res).mean()) / vol

    def calculate_position_size(self, volatility: float) -> float:
        """Calculate position size based on volatility"""
        return (self.f_risk * self.C_total) / (volatility * np.sqrt(self.T_max / self.N_yr))

    def execute_trade(self, date: pd.Timestamp, position_type: str, price: float) -> None:
        """Execute a trade with liquidity management"""
        daily_volume = self.stock_data.loc[date]
        slippage = 0.05 * (price / daily_volume) ** 1.5  # Slippage model

        executed_price = price + slippage if position_type == 'Long' else price - slippage
        self.positions.append({
            'entry_date': date,
            'position_type': position_type,
            'entry_price': executed_price,
            'status': 'Open',
            'exit_conditions': {
                'residual_threshold': self.theta_exit,
                'stop_loss': -0.02 * self.C_total,  # 2% of capital
                'profit_target': 0.03 * self.C_total,  # 3% of capital
                'max_days': self.T_max
            }
        })

    def monitor_positions(self, current_date: pd.Timestamp) -> None:
        """Monitor open positions and trigger exits"""
        for pos in self.positions:
            if pos['status'] == 'Open':
                days_in_trade = (current_date - pos['entry_date']).days
                current_price = self.stock_data.loc[current_date]

                exit_trigger = None
                pl = current_price - pos['entry_price'] if pos['position_type'] == 'Long' else pos[
                                                                                                   'entry_price'] - current_price

                if days_in_trade >= pos['exit_conditions']['max_days']:
                    exit_trigger = 'Time Exit'
                elif pl <= pos['exit_conditions']['stop_loss']:
                    exit_trigger = 'Stop Loss'
                elif pl >= pos['exit_conditions']['profit_target']:
                    exit_trigger = 'Profit Target'
                elif abs(self.residuals.loc[current_date]) <= self.theta_exit:
                    exit_trigger = 'Residual Exit'

                if exit_trigger:
                    pos['exit_date'] = current_date
                    pos['pl'] = pl - self.C_transaction
                    pos['status'] = 'Closed'
                    print(f"Position closed: {exit_trigger} | P&L: {pos['pl']:.2f}")

    def run_strategy(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Run the complete strategy"""
        self.download_data(start_date, end_date)
        etf_returns, stock_returns = self.calculate_returns()
        self.calculate_residuals(etf_returns, stock_returns)

        for date in self.residuals.index[self.W_res:]:
            current_residual = self.residuals[date]
            volatility = self.residuals.rolling(self.W_res).std().loc[date]

            position_size = self.calculate_position_size(volatility)

            if self.check_risk_constraints(position_size):
                if current_residual < -self.theta_entry:
                    self.execute_trade(date, 'Long', self.stock_data.loc[date])
                elif current_residual > self.theta_entry:
                    self.execute_trade(date, 'Short', self.stock_data.loc[date])

            self.monitor_positions(date)

        return self.generate_performance_report()

    def check_risk_constraints(self, position_size: float) -> bool:
        """Check portfolio risk constraints"""
        total_exposure = sum(pos['size'] for pos in self.positions if pos['status'] == 'Open')
        return (total_exposure + position_size) <= 0.3 * self.C_total

    def generate_performance_report(self) -> pd.DataFrame:
        """Generate detailed performance report"""
        pass


params = {
    'ETF': 'XLK',
    'Stock': 'MSFT',
    'C_total': 1_000_000,
    'theta_entry': 2.0,
    'theta_exit': 0.5,
    'W_beta': 60,
    'W_res': 90,
    'T_max': 21,
    'f_risk': 0.02,
    'theta_vol': 0.2,
    'C_transaction': 0.001
}

strategy = StatisticalArbitrageStrategy(params)
results = strategy.run_strategy('2020-01-01', '2024-01-01')
