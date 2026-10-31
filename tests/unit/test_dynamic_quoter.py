"""
Unit test suite for the dynamic inventory quoting and spread calculation engine.

Validates that mathematical formulas execute correctly across extreme conditions:
zero inventory, heavy crypto-skew, heavy fiat-skew, and sudden spot price drops,
ensuring bids and asks adhere to target net margin floors.
"""
