"""Generates the deobfuscated, final output image for each mode of protection.

Generating an deobfuscated output image is split into two stages:
  1. building the output image template
    - peutils
    - rebuilding the original import table
  2. generating all relocations in the output and applying fixups
    - global reloc template and information
    - fixups

Given that there are 3 distinct modes of operation that the obfuscator employs
there are subtle differences in how stage 1 is implemented. Those differences
are implemented in the `pefile_utils.py` file.
"""
from recover.recover_core import (
    ProtectionType,
    ProtectedInput64,
    RecoveredInstr,
    RecoveredFunc,
    x86,
    struct
)
import helpers.pefile_utils as peutils

def rebuild_output(
    d: ProtectedInput64,
    preserve_original_imports: bool = False
):
    """Build the final deobfuscated output based on the mode of protection.

    For HEADERLESS and FULL, given how we generate the output template and the
    how  the protection works, can always use the start of the .text section
    (0x1000) for the code region. For SELECTIVE, this will not be the case.
    The user needs to know the protected function upfront and specify it.

    """
    assert len(d.cfg.items()) != 0

    func_rva: int = 0x1000
    match d.protection_type:
        case ProtectionType.HEADERLESS:
            (
                output_pe,           # pefile
                d.import_to_rva_map  # dict[ApiName, RVA]
            ) = peutils.build_from_headerless_image_with_imports(d.jmppatchedbuffer,
                                                                 d.DATA_SECTION_EA,
                                                                 d.DATA_SECTION_SIZE,
                                                                 d.imp_dict_builder)
            d.newimgbuffer = output_pe.__data__

        case ProtectionType.SELECTIVE:
            assert d.selective_func_rva != -1
            (
                output_pe,            # pefile.PE  output binary full template
                d.import_to_rva_map   # dict[ApiName, RVA]
            ) = peutils.build_memory_image_with_imports(d.pe,
                                                        d.imp_dict_builder)
            d.newimgbuffer = output_pe.__data__
            #-------------------------------------------------------------------
            # find end region of the protected function and clear the region
            END_MARKER = bytes.fromhex("CC CC CC CC 66 66 0F 1F 84 00 00 00 00 00")
            start = d.selective_func_rva
            found = d.newimgbuffer.find(END_MARKER, start)
            if found == -1:
                #@TODO: fixme
                # raise ValueError("failed to find END_MARKER for protected function")
                found = 0x15311
            end  = found + len(END_MARKER)
            d.newimgbuffer[start:end] = bytearray(end-start)
            #-------------------------------------------------------------------
            d.func_rva = d.selective_func_rva

        case ProtectionType.FULL:
            (
                output_pe,            # pefile.PE  output binary full template
                d.import_to_rva_map   # dict[ApiName, RVA]
            ) = peutils.build_memory_image_with_imports(d.pe,
                                                        d.imp_dict_builder,
                                                        clear_text=True)
            d.newimgbuffer = output_pe.__data__
    #---------------------------------------------------------------------------
    Relocation.build_relocations(
        d,
        func_rva,
        preserve_original_imports)

#-------------------------------------------------------------------------------
def align_to_16_byte_boundary(value: int) -> int: return (value + 15) & ~15

class Relocation: # just a namespace

    @staticmethod
    def build_relocations( #@TODO: rename to `build_relocs_and_final_code_segment`
        d: ProtectedInput64,
        starting_off:int=0x1000,
        preserve_original_imports: bool=False
    ):
        """This routine is responsible for building the new code segment for the
        debofuscated binary and applying all relocations to it.

        It is responsible for building out the `global_relocs` map, which uses
        a unique tuple key per-instruction to lookup its relocated address. The
        tuple is composed of the following:
          - `func_ea`:     starting address of function an instruction is a
                           part of (it's all functions at the end of the day)
          - `instr_ea`:    original instruction address
          - `is_boundary`: is the instruction is a synthetic one that we
                           introduced to build function boundaries (normalization).
                           We have to track these as they have no actual original
                           address (they're synthetic) and this needs to be
                           accounted for during the relocation e.g. it will
                           only have a relocation address.

        After the new code segment is build alongside the global relocs map,
        fixups are applied to account for every relevant memory reference.

        The code section (.text) is assumed to be at  0x1000 given how we build
        the output template. SELECTIVE mode will have `starting_off` specific
        to the original starting address of the selected function that was
        protected. `preserve_original_imports also applies to SELECTIVE mode
        as it will contain a legitimate import table separate from the
        protected one.
        """
        #-----------------------------------------------------------------------
        # build the new relocs first
        curr_off = starting_off
        func_ea: int; rfn: RecoveredFunc
        for func_ea, rfn in d.cfg.items():
            rfn.reloc_ea = curr_off                                # relocated start ea
            d.global_relocs[(func_ea, func_ea, False)] = curr_off  # map in global lookup
            #-------------------------------------------------------------------
            r: RecoveredInstr
            for r in rfn.normalized_flow:
                d.global_relocs[(
                    func_ea,              # function instr is a part of
                    r.instr.ea,           # original instr location
                    r.is_boundary_jmp     # identifies whether instr is synthetic
                )] = curr_off
                r.reloc_ea = curr_off     # relocated instr ea
                #---------------------------------------------------------------
                ops = r.updated_bytes if r.updated_bytes else r.instr.bytes
                size = len(ops)
                #---------------------------------------------------------------
                d.newimgbuffer[curr_off:curr_off + size] = ops
                curr_off += size
            curr_off = align_to_16_byte_boundary(curr_off + 8)
        #-----------------------------------------------------------------------
        for rfn in d.cfg.values(): # rfn: RecoveredFunc
            try:
                Relocation.apply_all_fixups_to_rfn(d, rfn)
            except Exception as e:
                print(e)
        #-----------------------------------------------------------------------
        if preserve_original_imports:
            Relocation.preserve_original_imports(d)


    @staticmethod
    def apply_all_fixups_to_rfn(
        d: ProtectedInput64,
        rfn: RecoveredFunc
    ):
        """ Apply all known relocations to the new image, categorized in the
        following three formats:
            - control flow relocations
            - data flow relocations
            - import relocations
                - technically control flow as well, that are distinguished
                  on the fact that they're part of the obfuscator

        Because all uncovered samples were x64, relocation is exclusively limited
        to resolving the rip-relative addressing mode. It is the default mode in
        x64 binaries and virtually all data-referencing instructions (control flow
        instructions were already rip-relative since x86) will be in it.

        The signed displacement will always be the last 4 bytes outside of when
        an immediate operand exists alongside the displacement (the immediate
        granularity also plays into effect i.e., 8/16/32 (no 64-bit)):

            (c705d11d060001000000) mov dword ptr [rip+0x61dd1], 1
                C7 05 D1 1D 06 00 01 00 00 00
                :  :  :           :..IMM
                :  :  :..DISP
                :  :..MODRM
                :..OPCODE

            (48833d70e9030000) cmp qword ptr [rip + 0x3e970], 0
               48 83 3D 70 E9 03 00 00
               :  :  :  :           :..IMM
               :  :  :  :..DISP
               :  :  :..MODRM
               :  :..OPCODE
               :..REX

        """
        # @NOTE: assumes relocation already occured of the image buffer and each instruction
        #----------------------------------------------------------------------
        PACK_FIXUP = lambda fixup: bytearray(struct.pack("<I", fixup))
        CALC_FIXUP = lambda dest,size: (dest-(r.reloc_ea+size)) & 0xFFFFFFFF
        IS_IN_DATA = lambda dest: dest in d.data_range_rva

        def resolve_disp_fixup_and_apply(
            r: RecoveredInstr,
            dest: int
        ):
            """
            `reloc_ea` and `updated_bytes` are assumed to be valid
            The length of updated_bytes is only used in control flow
            """
            assert r.instr.disp_size == 4
            fixup = CALC_FIXUP(dest, r.instr.size)
            offset = r.instr.disp_offset
            r.updated_bytes[offset:offset+4] = PACK_FIXUP(fixup)

        def resolve_imm_fixup_and_apply(
            r: RecoveredInstr,
            reloc_dest: int,
        ):
            """call/jcc/jmp we handle ... add ...
            """
            assert (
                r.instr.is_call() or
                r.instr.is_jcc() or
                r.instr.is_jmp()
            )

            if r.instr.is_call():
                r.updated_bytes = d.ks.asm(
                    f'{r.instr.mnemonic} {reloc_dest:#08x}',
                    r.reloc_ea)[0]
            else:
                fixup = CALC_FIXUP(reloc_dest, len(r.updated_bytes))
                if r.instr.is_jmp():
                    assert len(r.updated_bytes) == 5 # placeholders
                    r.updated_bytes[1:5] = PACK_FIXUP(fixup)
                elif r.instr.is_jcc():
                    assert len(r.updated_bytes) == 6 # placeholders
                    r.updated_bytes[2:6] = PACK_FIXUP(fixup)

        def update_reloc_in_img(
            r: RecoveredInstr,
            tag: str
        ):
            """Assumes reloc_data to be valid

            """
            r.reloc_instr = d.md.decode_buffer(bytes(r.updated_bytes),
                                               r.reloc_ea)
            if len(r.updated_bytes) != r.reloc_instr.size:
                raise ValueError(
                    f'[Failed_{tag}_Reloc]: {r.func_start_ea:#08x}: '
                    f'{r.instr}, {r.reloc_instr}')
            d.newimgbuffer[r.reloc_ea:r.reloc_ea+r.reloc_instr.size] = (
                r.updated_bytes
            )
        """---------------------------------------------------------------------
        Imports
            d.imports:             dict[call/jmp addr]: RecoveredImport
            d.import_to_rva_map
        ---------------------------------------------------------------------"""
        r: RecoveredInstr
        for r in rfn.relocs_imports:
            if r.is_boundary_jmp:
                r.is_obf_import = False
                print(f'\tskipping synthetic jump that is linked to protected import {r}')
                continue

            r.updated_bytes = bytearray(r.instr.bytes)

            # @NOTE: `Empty` is for cases that are still not clear yet. They
            #        may not even be imports at all but basically they are
            #        a call+[rip+XXX] where the target is empty
            imp_entry = d.imports.get(r.instr.ea)
            if not imp_entry:
                input(f'[RelocImports] Could find imp entry for: {r}')
                continue
            elif imp_entry == 'Empty':
                continue

            imp_entry.new_rva = d.import_to_rva_map.get(imp_entry.api_name)
            if not imp_entry.new_rva: print(f'[RelocImports] Could find new rva for: {r}')

            resolve_disp_fixup_and_apply(r, imp_entry.new_rva)
            update_reloc_in_img(r, "Import")

            # @TODO: logging
            s = f'{imp_entry.dll_name}!{imp_entry.api_name}'
            print(f'\tRelocatedImport: {s:<40} {r.reloc_instr}')

        # DEBUG
        #open("BEFORE-TEST.exe", "wb").write(d.newimgbuffer)

        """---------------------------------------------------------------------
        ControlFlow

          - imports (ignore) identified here but already resolved
          - call
          - jcc/jmp
        update_bytes for is already set with the 6-byte variants with the
        displacement already padded with nops
        ---------------------------------------------------------------------"""
        for r in rfn.relocs_ctrlflow:
            if r.is_obf_import: continue

            dest = r.instr.get_op1_imm()
            reloc_dest = -1
            if r.instr.is_call():
                reloc_dest = d.global_relocs.get((dest, dest, False))
                if not reloc_dest:
                    raise ValueError(
                        f'[Call_Reloc] call: {r.instr} {dest:#08x} '
                        f'not relocated to {dest:08x}')
            else:
                reloc_dest = d.global_relocs.get((rfn.func_start_ea, dest, False))
                if not reloc_dest:
                    raise ValueError(
                        f'[JxxJmp_Reloc]: {r.func_start_ea:08x} '
                        f'{r.instr} {dest:#08x} {r.is_boundary_jmp}')
            assert(reloc_dest != -1)
            resolve_imm_fixup_and_apply(r, reloc_dest)
            update_reloc_in_img(r, tag="CtrlFlow")

        """---------------------------------------------------------------------
        DataFlow

        We track known data relocation instructions to be completely certain of
        which instructions are used here. Any new ones are trivially detectable
        and straightforward to add in.

        Displacements are generally the last 4-bytes of the instruction but not
        guaranteed i.e., immediates.

        For .data references, we don't need to fix anything up as the .data
        section is left untouched during the deobfuscation and kept at the
        same starting location.

        @TODO: utilty helper to identify all relocation instruction types
         identify_all_reloc_instr_types(relocs_s)
         {'and', 'cmove', 'cmp', 'inc', 'lea', 'mov'}

        Resolving data flow fixups amounts to:
          1. Identifying the operand with the displacment
            - this will differ depending on the access
            - capstone's "detail" does the heavy lifting here (`disp`)
          2. Using the displacement to calculate full destination target
            - capstone again (we wrap is with `disp_dest`)
          3. "Fixup" the resolved destination
          4. Patch the fixup at the right offset within the instruction
            - capstone again alleviates any burden's here with `disp_offset`
        ---------------------------------------------------------------------"""
        KNOWN = [
            x86.X86_INS_INC, x86.X86_INS_LEA, x86.X86_INS_CMOVE,
            x86.X86_INS_MOV, x86.X86_INS_CMP, x86.X86_INS_AND
        ]
        for r in rfn.relocs_dataflow:
            if not r.instr.id in KNOWN:
                print("[Missing dataflow instruction]: {r}")
                input("\tStopping iteration")
            r.updated_bytes = bytearray(r.instr.bytes)

            instr_tag = r.instr.mnemonic.upper()
            reloc_dest = r.instr.disp_dest
            if not IS_IN_DATA(reloc_dest):
                reloc_dest = d.global_relocs.get((reloc_dest,reloc_dest,False))
                if not reloc_dest: # no func pointer or missed recovery
                    raise ValueError(
                        f"[ResolveFixup_{instr_tag}] "
                        f"{r.func_start_ea:#08x} "
                        f"{r.instr} {reloc_dest:#08x}"
                    )
            resolve_disp_fixup_and_apply(r, reloc_dest)
            update_reloc_in_img(r, "DataFlow")

        '''
        for r in rfn.relocs_dataflow:
            #@TODO: do this somewhere else
            r.updated_bytes = bytearray(r.instr.bytes)
            match r.instr.id:
                case x86.X86_INS_INC:
                    """
                    R: 0x1979d [ff0511b20400]: inc dword ptr [rip + 0x4b211]>
                        FF 05 11 B2 04 00
                        :  :  :..DISP
                        :  :..MODRM
                        :..OPCODE
                    """
                    dest = r.instr.ea + r.instr.size + r.instr.Op1.mem.disp
                    assert dest == r.instr.disp_dest
                    fixup = resolve_fixup(r, dest, "INC")
                    update_displ_at_end(r, fixup)

                case x86.X86_INS_LEA:
                    """
                    R: 0x35ee5 [488d05ae0f0100]: lea rax, [rip + 0x10fae]>
                        48 8D 05 AE 0F 01 00
                        :  :  :  :..DISP
                        :  :  :..MODRM
                        :  :..OPCODE
                        :..REX
                    """
                    dest = r.instr.ea + r.instr.size + r.instr.Op2.mem.disp
                    assert dest == r.instr.disp_dest
                    fixup = resolve_fixup(r, dest, "LEA")
                    update_displ_at_end(r, fixup)

                case x86.X86_INS_CMOVE:
                    """
                    R: 0x137ef [480f441510160500]: cmove rdx, qword ptr [rip+0x51610]>
                        48 0F 44 15 10 16 05 00
                        :  :     :  :..DISP
                        :  :     :..MODRM
                        :  :..OPCODE
                        :..REX
                    """
                    dest = r.instr.ea + r.instr.size + r.instr.Op2.mem.disp
                    assert dest == r.instr.disp_dest
                    fixup = resolve_fixup(r, dest, "CMOVE")
                    update_displ_at_end(r, fixup)

                case x86.X86_INS_MOV:
                    """
                    R: 0x1dc26 [488b1503690400]: mov rdx, qword ptr [rip+0x46903]>
                        48 8B 15 03 69 04 00
                        :  :  :  :..DISP
                        :  :  :..MODRM
                        :  :..OPCODE
                        :..REX

                    R: 0x15a09 [488905f7f30400]: mov qword ptr [rip+0x4f3f7], rax>
                        48 89 05 F7 F3 04 00
                        :  :  :  :..DISP
                        :  :  :..MODRM
                        :  :..OPCODE
                        :..REX

                    R: 0x29ad  [c705d11d060001000000]: mov dword ptr [rip+0x61dd1], 1
                        C7 05 D1 1D 06 00 01 00 00 00
                        :  :  :           :..IMM
                        :  :  :..DISP
                        :  :..MODRM
                        :..OPCODE

                         0  MEM  W  MODRM_RM       32   TYPE  =     MEM
                                                        SEG   =      ds
                                                        BASE  =     rip
                                                        INDEX =    none
                                                        SCALE =       0
                                                        DISP  = 0x61DD1
                         1  IMM  R  SIMM16_32_32   32  [S A 32] 0x0000000000000001
                    """
                    dest = get_mem_disp(r) + r.instr.size + r.instr.ea
                    assert dest == r.instr.disp_dest
                    fixup = resolve_fixup(r, dest, "MOV")
                    update_displ_with_imm(r, fixup)

                case x86.X86_INS_CMP:
                    """
                    0x111de (4c392d3f480500)    cmp qword ptr [rip + 0x5483f], r13>
                        4C 39 2D 3F 48 05 00
                        :  :  :  :..DISP
                        :  :  :..MODRM
                        :  :..OPCODE
                        :..REX

                    0x30372 (483b0d8e4a0300)    cmp rcx, qword ptr [rip + 0x34a8e]>
                        48 3B 0D 8E 4A 03 00
                        :  :  :  :..DISP
                        :  :  :..MODRM
                        :  :..OPCODE
                        :..REX

                    (8+) with immediate
                    0x04915d (48833d70e9030000) cmp qword ptr [rip + 0x3e970], 0
                       48 83 3D 70 E9 03 00 00
                       :  :  :  :           :..IMM
                       :  :  :  :..DISP
                       :  :  :..MODRM
                       :  :..OPCODE
                       :..REX

                    (non-REX prefix)
                    0x073aa7 (6639357b250100)   cmp word ptr [rip + 0x1257b], si
                       66 39 35 7B 25 01 00
                       :  :  :  :..DISP
                       :  :  :..MODRM
                       :  :..OPCODE
                       :..PREFIXES
                       
                    (reg, non-prefix)
                    0x02ba82 (391d36b20300)     cmp dword ptr [rip + 0x3b236], ebx
                       39 1D 36 B2 03 00
                       :  :  :..DISP
                       :  :..MODRM
                       :..OPCODE

                    (reg, prefix)
                    4C 39 3D 55 F5 05 00
                    :  :  :  :..DISP
                    :  :  :..MODRM
                    :  :..OPCODE
                    :..REX
                    """
                    #----------------------------------------------------------
                    dest = get_mem_disp(r) + r.instr.size + r.instr.ea
                    assert dest == r.instr.disp_dest
                    fixup = resolve_fixup(r, dest, "CMP")
                    update_displ_with_imm(r, fixup)

                case x86.X86_INS_AND:
                    """
                    R: 0x2f256 [83255357030000]: and dword ptr [rip + 0x35753], 0>
                        83 25 53 57 03 00 00
                        :  :  :           :..IMM
                        :  :  :..DISP
                        :  :..MODRM
                        :..OPCODE

                    R: 0x2c5ed [213dbd830300]: and dword ptr [rip + 0x383bd], edi>
                        21 3D BD 83 03 00
                        :  :  :..DISP
                        :  :..MODRM
                        :..OPCODE

                     0   MEM  RW  MODRM_RM   32 TYPE  =      MEM
                                                SEG   =       ds
                                                BASE  =      rip
                                                INDEX =     none
                                                SCALE =        0
                                                DISP  =  0x383BD
                     1   REG  R   MODRM_REG  32              edi
    '''

    @staticmethod
    def preserve_original_imports(
        d: ProtectedInput64
    ):
        for instr_ea, (api_name, instr_size) in d.imports_to_preserve.items():
            new_rva = d.import_to_rva_map.get(api_name)
            if not new_rva:
                d.log.info(f'Preserving import: {instr_ea:#x} {api_name}')
            fixup = (new_rva - (instr_ea + instr_size)) & 0xFFFFFFFF
            d.newimgbuffer[instr_ea+2:instr_ea+6] = (
                bytearray(struct.pack("<I", fixup))
            )

