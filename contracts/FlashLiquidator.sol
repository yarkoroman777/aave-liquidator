// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "https://github.com/aave/aave-v3-core/blob/master/contracts/flashloan/interfaces/IFlashLoanSimpleReceiver.sol";
import "https://github.com/aave/aave-v3-core/blob/master/contracts/interfaces/IPool.sol";
import "https://github.com/Uniswap/v3-periphery/blob/main/contracts/interfaces/ISwapRouter.sol";
import "https://github.com/OpenZeppelin/openzeppelin-contracts/blob/master/contracts/token/ERC20/IERC20.sol";

contract FlashLiquidator is IFlashLoanSimpleReceiver {
    address public owner;
    address public aavePool;
    address public uniswapRouter;
    address public coldWallet;
    
    struct LiquidationData {
        address user;
        address debtAsset;
        address collateralAsset;
        uint256 debtAmount;
        uint256 profit;
    }
    LiquidationData private liquidationData;
    
    event Liquidated(address indexed user, uint256 debtAmount, uint256 profit);
    
    constructor(address _aavePool, address _uniswapRouter, address _coldWallet) {
        owner = msg.sender;
        aavePool = _aavePool;
        uniswapRouter = _uniswapRouter;
        coldWallet = _coldWallet;
    }
    
    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }
    
    function liquidate(
        address user,
        address debtAsset,
        address collateralAsset,
        uint256 debtAmount
    ) external onlyOwner {
        require(debtAmount > 0, "Zero debt");
        liquidationData = LiquidationData(user, debtAsset, collateralAsset, debtAmount, 0);
        IPool(aavePool).flashLoanSimple(
            address(this),
            debtAsset,
            debtAmount,
            "",
            0
        );
    }
    
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == aavePool, "Not Aave pool");
        require(initiator == address(this), "Not from this contract");
        
        IPool(aavePool).liquidationCall(
            liquidationData.collateralAsset,
            liquidationData.debtAsset,
            liquidationData.user,
            liquidationData.debtAmount,
            false
        );
        
        uint256 collateralBalance = IERC20(liquidationData.collateralAsset).balanceOf(address(this));
        
        if (liquidationData.collateralAsset != liquidationData.debtAsset) {
            IERC20(liquidationData.collateralAsset).approve(uniswapRouter, collateralBalance);
            ISwapRouter.ExactInputSingleParams memory swapParams = ISwapRouter.ExactInputSingleParams({
                tokenIn: liquidationData.collateralAsset,
                tokenOut: liquidationData.debtAsset,
                fee: 3000,
                recipient: address(this),
                deadline: block.timestamp + 300,
                amountIn: collateralBalance,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            });
            ISwapRouter(uniswapRouter).exactInputSingle(swapParams);
        }
        
        uint256 amountOwed = amount + premium;
        IERC20(liquidationData.debtAsset).approve(aavePool, amountOwed);
        
        uint256 remaining = IERC20(liquidationData.debtAsset).balanceOf(address(this)) - amountOwed;
        liquidationData.profit = remaining;
        
        if (remaining > 0) {
            IERC20(liquidationData.debtAsset).transfer(coldWallet, remaining);
        }
        
        emit Liquidated(liquidationData.user, liquidationData.debtAmount, remaining);
        return true;
    }
    
    receive() external payable {}
}
