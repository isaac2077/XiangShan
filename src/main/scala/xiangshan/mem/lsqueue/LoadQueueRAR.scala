/***************************************************************************************
* Copyright (c) 2020-2021 Institute of Computing Technology, Chinese Academy of Sciences
* Copyright (c) 2020-2021 Peng Cheng Laboratory
*
* XiangShan is licensed under Mulan PSL v2.
* You can use this software according to the terms and conditions of the Mulan PSL v2.
* You may obtain a copy of Mulan PSL v2 at:
*          http://license.coscl.org.cn/MulanPSL2
*
* THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
* EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
* MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
*
* See the Mulan PSL v2 for more details.
***************************************************************************************/
package xiangshan.mem

import org.chipsalliance.cde.config._
import chisel3._
import chisel3.util._
import utils._
import utility._
import xiangshan._
import xiangshan.backend.rob.RobPtr
import xiangshan.backend.Bundles.DynInst
import xiangshan.mem.Bundles._
import xiangshan.cache._

class LoadQueueRAR(implicit p: Parameters) extends XSModule
  with HasDCacheParameters
  with HasCircularQueuePtrHelper
  with HasLoadHelper
  with HasPerfEvents
{
  val io = IO(new Bundle() {
    // control
    val redirect = Flipped(Valid(new Redirect))

    // violation query
    val query = Vec(LoadPipelineWidth, Flipped(new LoadNukeQueryIO))

    // release cacheline
    val release = Flipped(Valid(new Release))

    // from VirtualLoadQueue
    val ldWbPtr = Input(new LqPtr)

    // global
    val lqFull = Output(Bool())

    val validCount = Output(UInt())
  })

  private val PartialPAddrStride: Int = 6
  private val PartialPAddrBits: Int = 16
  private val PartialPAddrLowBits: Int = (PartialPAddrBits - PartialPAddrStride) / 2 // avoid overlap
  private val PartialPAddrHighBits: Int = PartialPAddrBits - PartialPAddrLowBits
  private def boundary(x: Int, h: Int) = if (x < h) Some(x) else None
  private def lowMapping = (0 until PartialPAddrLowBits).map(i => Seq(
      boundary(PartialPAddrStride + i  , PartialPAddrBits),
      boundary(PartialPAddrBits - i - 1, PartialPAddrBits)
    )
  )
  private def highMapping = (0 until PartialPAddrHighBits).map(i => Seq(
      boundary(i + PartialPAddrStride     , PAddrBits),
      boundary(i + PartialPAddrStride + 11, PAddrBits),
      boundary(i + PartialPAddrStride + 22, PAddrBits),
      boundary(i + PartialPAddrStride + 33, PAddrBits)
    )
  )
  private def genPartialPAddr(paddr: UInt) = {
    val ppaddr_low = Wire(Vec(PartialPAddrLowBits, Bool()))
    ppaddr_low.zip(lowMapping).foreach {
      case (bit, mapping) =>
        bit := mapping.filter(_.isDefined).map(x => paddr(x.get)).reduce(_^_)
    }

    val ppaddr_high = Wire(Vec(PartialPAddrHighBits, Bool()))
    ppaddr_high.zip(highMapping).foreach {
      case (bit, mapping) =>
        bit := mapping.filter(_.isDefined).map(x => paddr(x.get)).reduce(_^_)
    }
    Cat(ppaddr_high.asUInt, ppaddr_low.asUInt)
  }

  println("LoadQueueRAR: size: " + LoadQueueRARSize)
  //  LoadQueueRAR field
  //  +-------+-------+-------+----------+
  //  | Valid |  Uop  | PAddr | Released |
  //  +-------+-------+-------+----------+
  //
  //  Field descriptions:
  //  Allocated   : entry is valid.
  //  MicroOp     : Micro-op
  //  PAddr       : physical address.
  //  Released    : DCache released.
  val allocated = RegInit(VecInit(List.fill(LoadQueueRARSize)(false.B))) // The control signals need to explicitly indicate the initial value
  val uop = Reg(Vec(LoadQueueRARSize, new DynInst))
  val paddrModule = Module(new LqPAddrModule(
    gen = UInt(PartialPAddrBits.W),
    numEntries = LoadQueueRARSize,
    numRead = LoadPipelineWidth,
    numWrite = LoadPipelineWidth,
    numWBank = LoadQueueNWriteBanks,
    numWDelay = 1,
    numCamPort = LoadPipelineWidth,
    enableCacheLineCheck = false, // Now `RARQueue` has no need to check cacheline.
    paddrOffset = 0 // If you need to check cacheline, set the offset relative to the original paddr correctly.
  ))
  paddrModule.io := DontCare
  val released = RegInit(VecInit(List.fill(LoadQueueRARSize)(false.B)))

  // freeliset: store valid entries index.
  // +---+---+--------------+-----+-----+
  // | 0 | 1 |      ......  | n-2 | n-1 |
  // +---+---+--------------+-----+-----+
  val freeList = Module(new FreeList(
    size = LoadQueueRARSize,
    allocWidth = LoadPipelineWidth,
    freeWidth = 4,
    enablePreAlloc = false,
    moduleName = "LoadQueueRAR freelist"
  ))
  freeList.io := DontCare

  // Real-allocation: load_s2
  // PAddr write needs 2 cycles, release signal should delay 1 cycle so that
  // load enqueue can catch release.
  val release1Cycle = io.release
  // val release2Cycle = RegNext(io.release)
  // val release2Cycle_dup_lsu = RegNext(io.release)
  val release2Cycle = RegEnable(io.release, io.release.valid)
  release2Cycle.valid := RegNext(io.release.valid)
  //val release2Cycle_dup_lsu = RegEnable(io.release, io.release.valid)

  // LoadQueueRAR enqueue condition:
  // There are still not completed load instructions before the current load instruction.
  // (e.g. "not completed" means that load instruction get the data or exception).
  val canEnqueue = io.query.map(_.req.valid)
  val cancelEnqueue = io.query.map(_.req.bits.uop.robIdx.needFlush(io.redirect))
  val hasNotWritebackedLoad = io.query.map(_.req.bits.uop.lqIdx).map(lqIdx => isAfter(lqIdx, io.ldWbPtr))
  val needEnqueue = canEnqueue.zip(hasNotWritebackedLoad).zip(cancelEnqueue).map { case ((v, r), c) => v && r && !c }

  // Allocate logic
  val acceptedVec = Wire(Vec(LoadPipelineWidth, Bool()))
  val enqIndexVec = Wire(Vec(LoadPipelineWidth, UInt(log2Up(LoadQueueRARSize).W)))

  for ((enq, w) <- io.query.map(_.req).zipWithIndex) {
    acceptedVec(w) := false.B
    paddrModule.io.wen(w) := false.B
    freeList.io.doAllocate(w) := false.B

    freeList.io.allocateReq(w) := true.B

    //  Allocate ready
    val offset = PopCount(needEnqueue.take(w))
    val canAccept = freeList.io.canAllocate(offset)
    val enqIndex = freeList.io.allocateSlot(offset)
    enq.ready := Mux(needEnqueue(w), canAccept, true.B)

    enqIndexVec(w) := enqIndex
    when (needEnqueue(w) && enq.ready) {
      acceptedVec(w) := true.B

      freeList.io.doAllocate(w) := true.B
      //  Allocate new entry
      allocated(enqIndex) := true.B

      //  Write paddr
      paddrModule.io.wen(w) := true.B
      paddrModule.io.waddr(w) := enqIndex
      paddrModule.io.wdata(w) := genPartialPAddr(enq.bits.paddr)

      //  Fill info
      uop(enqIndex) := enq.bits.uop
      //  NC is uncachable and will not be explicitly released.
      //  So NC requests are not allowed to have RAR
      released(enqIndex) := enq.bits.is_nc || (
        enq.bits.data_valid &&
        (release2Cycle.valid &&
        enq.bits.paddr(PAddrBits-1, DCacheLineOffset) === release2Cycle.bits.paddr(PAddrBits-1, DCacheLineOffset) ||
        release1Cycle.valid &&
        enq.bits.paddr(PAddrBits-1, DCacheLineOffset) === release1Cycle.bits.paddr(PAddrBits-1, DCacheLineOffset))
      )
    }
    val debug_robIdx = enq.bits.uop.robIdx.asUInt
    XSError(
      needEnqueue(w) && enq.ready && allocated(enqIndex),
      p"LoadQueueRAR: You can not write an valid entry! check: ldu $w, robIdx $debug_robIdx")
  }

  //  LoadQueueRAR deallocate
  val freeMaskVec = Wire(Vec(LoadQueueRARSize, Bool()))

  // init
  freeMaskVec.map(e => e := false.B)

  // when the loads that "older than" current load were writebacked,
  // current load will be released.
  for (i <- 0 until LoadQueueRARSize) {
    val deqNotBlock = !isBefore(io.ldWbPtr, uop(i).lqIdx)
    val needFlush = uop(i).robIdx.needFlush(io.redirect)

    when (allocated(i) && (deqNotBlock || needFlush)) {
      allocated(i) := false.B
      freeMaskVec(i) := true.B
    }
  }

  // if need replay revoke entry
  val lastCanAccept = GatedRegNext(acceptedVec)
  val lastAllocIndex = GatedRegNext(enqIndexVec)

  for ((revoke, w) <- io.query.map(_.revoke).zipWithIndex) {
    val revokeValid = revoke && lastCanAccept(w)
    val revokeIndex = lastAllocIndex(w)

    when (allocated(revokeIndex) && revokeValid) {
      allocated(revokeIndex) := false.B
      freeMaskVec(revokeIndex) := true.B
    }
  }

  freeList.io.free := freeMaskVec.asUInt

  // LoadQueueRAR Query
  // Load-to-Load violation check condition:
  // 1. Physical address match by CAM port.
  // 2. release or nc_with_data is set.
  // 3. Younger than current load instruction.
  val ldLdViolation = Wire(Vec(LoadPipelineWidth, Bool()))
  //val allocatedUInt = RegNext(allocated.asUInt)
  for ((query, w) <- io.query.zipWithIndex) {
    ldLdViolation(w) := false.B
    paddrModule.io.releaseViolationMdata(w) := genPartialPAddr(query.req.bits.paddr)

    query.resp.valid := RegNext(query.req.valid)
    // Generate real violation mask
    val robIdxMask = VecInit(uop.map(_.robIdx).map(isAfter(_, query.req.bits.uop.robIdx)))
    val matchMaskReg = Wire(Vec(LoadQueueRARSize, Bool()))
    for(i <- 0 until LoadQueueRARSize) {
      matchMaskReg(i) := (allocated(i) &
                         paddrModule.io.releaseViolationMmask(w)(i) &
                         robIdxMask(i) &&
                         released(i))
      }
    val matchMask = GatedValidRegNext(matchMaskReg)
    //  Load-to-Load violation check result
    val ldLdViolationMask = matchMask
    ldLdViolationMask.suggestName("ldLdViolationMask_" + w)
    query.resp.bits.rep_frm_fetch := ParallelORR(ldLdViolationMask)
  }


  // When io.release.valid (release1cycle.valid), it uses the last ld-ld paddr cam port to
  // update release flag in 1 cycle
  val releaseVioMask = Reg(Vec(LoadQueueRARSize, Bool()))
  when (release1Cycle.valid) {
    paddrModule.io.releaseMdata.takeRight(1)(0) := genPartialPAddr(release1Cycle.bits.paddr)
  }

  (0 until LoadQueueRARSize).map(i => {
    when (RegNext((paddrModule.io.releaseMmask.takeRight(1)(0)(i)) && allocated(i) && release1Cycle.valid)) {
      // Note: if a load has missed in dcache and is waiting for refill in load queue,
      // its released flag still needs to be set as true if addr matches.
      released(i) := true.B
    }
  })

  io.lqFull := freeList.io.empty
  io.validCount := freeList.io.validCount

  // perf cnt
  val canEnqCount = PopCount(io.query.map(_.req.fire))
  val validCount = freeList.io.validCount
  val allowEnqueue = validCount <= (LoadQueueRARSize - LoadPipelineWidth).U
  val ldLdViolationCount = PopCount(io.query.map(_.resp).map(resp => resp.valid && resp.bits.rep_frm_fetch))

  QueuePerf(LoadQueueRARSize, validCount, !allowEnqueue)
  XSPerfAccumulate("enq", canEnqCount)
  XSPerfAccumulate("ld_ld_violation", ldLdViolationCount)
  val perfEvents: Seq[(String, UInt)] = Seq(
    ("enq", canEnqCount),
    ("ld_ld_violation", ldLdViolationCount)
  )
  generatePerfEvent()
  // End
}
