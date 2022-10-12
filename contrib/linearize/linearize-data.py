#!/usr/bin/python
#
# linearize-data.py: Construct a linear, no-fork version of the chain.
#
# Copyright (c) 2013-2014 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

from __future__ import print_function, division
import json
import struct
import re
import os
import os.path
import base64
import httplib
import sys
import hashlib
import datetime
import time
from collections import namedtuple

settings = {}

def uint32(x):
	return x & 0xffffffffL

def bytereverse(x):
	return uint32(( ((x) << 24) | (((x) << 8) & 0x00ff0000) |
		       (((x) >> 8) & 0x0000ff00) | ((x) >> 24) ))

def bufreverse(in_buf):
	out_words = []
	for i in range(0, len(in_buf), 4):
		word = struct.unpack('@I', in_buf[i:i+4])[0]
		out_words.append(struct.pack('@I', bytereverse(word)))
	return ''.join(out_words)

def wordreverse(in_buf):
	out_words = []
	for i in range(0, len(in_buf), 4):
		out_words.append(in_buf[i:i+4])
	out_words.reverse()
	return ''.join(out_words)

def calc_hdr_hash(mkt_hdr):
	hash1 = hashlib.sha256()
	hash1.update(mkt_hdr)
	hash1_o = hash1.digest()

	hash2 = hashlib.sha256()
	hash2.update(hash1_o)
	hash2_o = hash2.digest()

	return hash2_o

def calc_hash_str(mkt_hdr):
	hash = calc_hdr_hash(mkt_hdr)
	hash = bufreverse(hash)
	hash = wordreverse(hash)
	hash_str = hash.encode('hex')
	return hash_str

def get_mkt_dt(mkt_hdr):
	members = struct.unpack("<I", mkt_hdr[68:68+4])
	nTime = members[0]
	dt = datetime.datetime.fromtimestamp(nTime)
	dt_ym = datetime.datetime(dt.year, dt.month, 1)
	return (dt_ym, nTime)

def get_block_hashes(settings):
	mktindex = []
	f = open(settings['hashlist'], "r")
	for line in f:
		line = line.rstrip()
		mktindex.append(line)

	print("Read " + str(len(mktindex)) + " hashes")

	return mktindex

def mkblockmap(mktindex):
	mktmap = {}
	for height,hash in enumerate(mktindex):
		mktmap[hash] = height
	return mktmap

# Block header and extent on disk
BlockExtent = namedtuple('BlockExtent', ['fn', 'offset', 'inhdr', 'mkthdr', 'size'])

class BlockDataCopier:
	def __init__(self, settings, mktindex, mktmap):
		self.settings = settings
		self.mktindex = mktindex
		self.mktmap = mktmap

		self.inFn = 0
		self.inF = None
		self.outFn = 0
		self.outsz = 0
		self.outF = None
		self.outFname = None
		self.mktCountIn = 0
		self.mktCountOut = 0

		self.lastDate = datetime.datetime(2000, 1, 1)
		self.highTS = 1408893517 - 315360000
		self.timestampSplit = False
		self.fileOutput = True
		self.setFileTime = False
		self.maxOutSz = settings['max_out_sz']
		if 'output' in settings:
			self.fileOutput = False
		if settings['file_timestamp'] != 0:
			self.setFileTime = True
		if settings['split_timestamp'] != 0:
			self.timestampSplit = True
		# Extents and cache for out-of-order blocks
		self.blockExtents = {}
		self.outOfOrderData = {}
		self.outOfOrderSize = 0 # running total size for items in outOfOrderData

	def writeBlock(self, inhdr, mkt_hdr, rawblock):
		blockSizeOnDisk = len(inhdr) + len(mkt_hdr) + len(rawblock)
		if not self.fileOutput and ((self.outsz + blockSizeOnDisk) > self.maxOutSz):
			self.outF.close()
			if self.setFileTime:
				os.utime(outFname, (int(time.time()), highTS))
			self.outF = None
			self.outFname = None
			self.outFn = self.outFn + 1
			self.outsz = 0

		(mktDate, mktTS) = get_mkt_dt(mkt_hdr)
		if self.timestampSplit and (mktDate > self.lastDate):
			print("New month " + mktDate.strftime("%Y-%m") + " @ " + hash_str)
			lastDate = mktDate
			if outF:
				outF.close()
				if setFileTime:
					os.utime(outFname, (int(time.time()), highTS))
				self.outF = None
				self.outFname = None
				self.outFn = self.outFn + 1
				self.outsz = 0

		if not self.outF:
			if self.fileOutput:
				outFname = self.settings['output_file']
			else:
				outFname = os.path.join(self.settings['output'], "mkt%05d.dat" % self.outFn)
			print("Output file " + outFname)
			self.outF = open(outFname, "wb")

		self.outF.write(inhdr)
		self.outF.write(mkt_hdr)
		self.outF.write(rawblock)
		self.outsz = self.outsz + len(inhdr) + len(mkt_hdr) + len(rawblock)

		self.mktCountOut = self.mktCountOut + 1
		if mktTS > self.highTS:
			self.highTS = mktTS

		if (self.mktCountOut % 1000) == 0:
			print('%i blocks scanned, %i blocks written (of %i, %.1f%% complete)' % 
					(self.mktCountIn, self.mktCountOut, len(self.mktindex), 100.0 * self.mktCountOut / len(self.mktindex)))

	def inFileName(self, fn):
		return os.path.join(self.settings['input'], "mkt%05d.dat" % fn)

	def fetchBlock(self, extent):
		'''Fetch block contents from disk given extents'''
		with open(self.inFileName(extent.fn), "rb") as f:
			f.seek(extent.offset)
			return f.read(extent.size)

	def copyOneBlock(self):
		'''Find the next block to be written in the input, and copy it to the output.'''
		extent = self.blockExtents.pop(self.mktCountOut)
		if self.mktCountOut in self.outOfOrderData:
			# If the data is cached, use it from memory and remove from the cache
			rawblock = self.outOfOrderData.pop(self.mktCountOut)
			self.outOfOrderSize -= len(rawblock)
		else: # Otherwise look up data on disk
			rawblock = self.fetchBlock(extent)

		self.writeBlock(extent.inhdr, extent.mkthdr, rawblock)

	def run(self):
		while self.mktCountOut < len(self.mktindex):
			if not self.inF:
				fname = self.inFileName(self.inFn)
				print("Input file " + fname)
				try:
					self.inF = open(fname, "rb")
				except IOError:
					print("Premature end of block data")
					return

			inhdr = self.inF.read(8)
			if (not inhdr or (inhdr[0] == "\0")):
				self.inF.close()
				self.inF = None
				self.inFn = self.inFn + 1
				continue

			inMagic = inhdr[:4]
			if (inMagic != self.settings['netmagic']):
				print("Invalid magic: " + inMagic.encode('hex'))
				return
			inLenLE = inhdr[4:]
			su = struct.unpack("<I", inLenLE)
			inLen = su[0] - 80 # length without header
			mkt_hdr = self.inF.read(80)
			inExtent = BlockExtent(self.inFn, self.inF.tell(), inhdr, mkt_hdr, inLen)

			hash_str = calc_hash_str(mkt_hdr)
			if not hash_str in mktmap:
				print("Skipping unknown block " + hash_str)
				self.inF.seek(inLen, os.SEEK_CUR)
				continue

			mktHeight = self.mktmap[hash_str]
			self.mktCountIn += 1

			if self.mktCountOut == mktHeight:
				# If in-order block, just copy
				rawblock = self.inF.read(inLen)
				self.writeBlock(inhdr, mkt_hdr, rawblock)

				# See if we can catch up to prior out-of-order blocks
				while self.mktCountOut in self.blockExtents:
					self.copyOneBlock()

			else: # If out-of-order, skip over block data for now
				self.blockExtents[mktHeight] = inExtent
				if self.outOfOrderSize < self.settings['out_of_order_cache_sz']:
					# If there is space in the cache, read the data
					# Reading the data in file sequence instead of seeking and fetching it later is preferred,
					# but we don't want to fill up memory
					self.outOfOrderData[mktHeight] = self.inF.read(inLen)
					self.outOfOrderSize += inLen
				else: # If no space in cache, seek forward
					self.inF.seek(inLen, os.SEEK_CUR)

		print("Done (%i blocks written)" % (self.mktCountOut))

if __name__ == '__main__':
	if len(sys.argv) != 2:
		print("Usage: linearize-data.py CONFIG-FILE")
		sys.exit(1)

	f = open(sys.argv[1])
	for line in f:
		# skip comment lines
		m = re.search('^\s*#', line)
		if m:
			continue

		# parse key=value lines
		m = re.search('^(\w+)\s*=\s*(\S.*)$', line)
		if m is None:
			continue
		settings[m.group(1)] = m.group(2)
	f.close()

	if 'netmagic' not in settings:
		settings['netmagic'] = 'f9beb4d9'
	if 'genesis' not in settings:
		settings['genesis'] = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'
	if 'input' not in settings:
		settings['input'] = 'input'
	if 'hashlist' not in settings:
		settings['hashlist'] = 'hashlist.txt'
	if 'file_timestamp' not in settings:
		settings['file_timestamp'] = 0
	if 'split_timestamp' not in settings:
		settings['split_timestamp'] = 0
	if 'max_out_sz' not in settings:
		settings['max_out_sz'] = 1000L * 1000 * 1000
	if 'out_of_order_cache_sz' not in settings:
		settings['out_of_order_cache_sz'] = 100 * 1000 * 1000

	settings['max_out_sz'] = long(settings['max_out_sz'])
	settings['split_timestamp'] = int(settings['split_timestamp'])
	settings['file_timestamp'] = int(settings['file_timestamp'])
	settings['netmagic'] = settings['netmagic'].decode('hex')
	settings['out_of_order_cache_sz'] = int(settings['out_of_order_cache_sz'])

	if 'output_file' not in settings and 'output' not in settings:
		print("Missing output file / directory")
		sys.exit(1)

	mktindex = get_block_hashes(settings)
	mktmap = mkblockmap(mktindex)

	if not settings['genesis'] in mktmap:
		print("Genesis block not found in hashlist")
	else:
		BlockDataCopier(settings, mktindex, mktmap).run()


