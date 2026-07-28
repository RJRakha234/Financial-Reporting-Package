Attribute VB_Name = "FinRound"
Option Explicit
'==============================================================================
' FinRound - controlled rounding of a table so that its totals keep footing.
'
' Round a table figure by figure and it stops adding up: three lines of 33.3
' round to 33 each, but their total of 99.9 rounds to 100. This module rounds
' the whole table at once, so that every figure lands on one of the two
' multiples of the step either side of its true value AND every total,
' subtotal, row total, column total and grand total is exactly the sum of its
' rounded components - down the rows and across the columns at the same time.
'
' A block of rows against columns is a flow network: a unit of flow from row i
' into column j is a unit in cell (i, j), conservation at the nodes IS the
' totals footing, and arc bounds [floor, ceil] are the "stay adjacent" rule.
' Network integrality guarantees a consistent rounding exists; minimum cost
' picks the cheapest one. Nested subtotals become a cascade of blocks solved
' outermost first, so aggregates keep their own nearest value and any
' compromise is pushed down to the least aggregated figures.
'
' This module is pure Basic - no Excel objects - so it can be tested and
' reused anywhere. FinRoundExcel.bas is the worksheet front end.
'
' Everything is measured in "ticks": ten-thousandths of a rounding step. Values
' are held in Double, which represents whole numbers exactly to 2^53, so all
' rounding decisions are made in exact integer arithmetic.
'==============================================================================

Private Const FR_TICKS As Double = 10000#
Private Const FR_INF As Double = 1E+300
Private Const FR_MAX_SLACK As Long = 3
Private Const FR_REL_EPS As Double = 0.000000001

Private Type FRSpan
    Lo As Double          ' fewest whole steps this quantity may take
    Hi As Double          ' most it may take
    Target As Double      ' the true value, in ticks
    Costed As Boolean     ' False: any value in range will do, at no cost
End Type

'--- network state ------------------------------------------------------------
Private nNode As Long
Private nArc As Long
Private arcTo() As Long
Private arcNext() As Long
Private arcCap() As Double
Private arcCost() As Double
Private arcHead() As Long

Private nEdge As Long
Private edgeU() As Long
Private edgeV() As Long
Private edgeLo() As Double
Private edgeHi() As Double
Private edgeCost() As Double
Private edgeArc() As Long
Private edgeBase() As Double
Private edgeSign() As Double
Private edgeCap0() As Double

'--- last-run diagnostics, for the report ------------------------------------
Public FRLastSlack As Long
Public FRLastMessage As String

'==============================================================================
' Minimum-cost integral circulation with lower bounds
'==============================================================================

Private Sub NetInit(ByVal nodes As Long)
    nNode = nodes
    nEdge = 0
    ReDim edgeU(0 To 63)
    ReDim edgeV(0 To 63)
    ReDim edgeLo(0 To 63)
    ReDim edgeHi(0 To 63)
    ReDim edgeCost(0 To 63)
End Sub

Private Function NetAddEdge(ByVal u As Long, ByVal v As Long, ByVal lo As Double, _
                            ByVal hi As Double, ByVal cost As Double) As Long
    If nEdge > UBound(edgeU) Then
        Dim grown As Long
        grown = (UBound(edgeU) + 1) * 2 - 1
        ReDim Preserve edgeU(0 To grown)
        ReDim Preserve edgeV(0 To grown)
        ReDim Preserve edgeLo(0 To grown)
        ReDim Preserve edgeHi(0 To grown)
        ReDim Preserve edgeCost(0 To grown)
    End If
    edgeU(nEdge) = u
    edgeV(nEdge) = v
    edgeLo(nEdge) = lo
    edgeHi(nEdge) = hi
    edgeCost(nEdge) = cost
    NetAddEdge = nEdge
    nEdge = nEdge + 1
End Function

Private Function AddArc(ByVal u As Long, ByVal v As Long, ByVal cap As Double, _
                        ByVal cost As Double) As Long
    arcTo(nArc) = v
    arcCap(nArc) = cap
    arcCost(nArc) = cost
    arcNext(nArc) = arcHead(u)
    arcHead(u) = nArc
    arcTo(nArc + 1) = u
    arcCap(nArc + 1) = 0
    arcCost(nArc + 1) = -cost
    arcNext(nArc + 1) = arcHead(v)
    arcHead(v) = nArc + 1
    AddArc = nArc
    nArc = nArc + 2
End Function

' Subtracting each arc's lower bound leaves an imbalance at its endpoints that
' a super source and sink must absorb. An arc whose cost is negative is
' saturated first - flow on it is a saving, so it is taken to the limit and
' only given back where the constraints demand - which leaves every remaining
' cost non-negative, as Dijkstra requires.
Private Function NetSolve() As Boolean
    Dim total As Long, src As Long, snk As Long, e As Long, n As Long
    total = nNode + 2
    src = nNode
    snk = nNode + 1

    nArc = 0
    ReDim arcTo(0 To 2 * nEdge + 2 * total + 4)
    ReDim arcNext(0 To 2 * nEdge + 2 * total + 4)
    ReDim arcCap(0 To 2 * nEdge + 2 * total + 4)
    ReDim arcCost(0 To 2 * nEdge + 2 * total + 4)
    ReDim arcHead(0 To total - 1)
    For n = 0 To total - 1
        arcHead(n) = -1
    Next n

    ReDim edgeArc(0 To nEdge)
    ReDim edgeBase(0 To nEdge)
    ReDim edgeSign(0 To nEdge)
    ReDim edgeCap0(0 To nEdge)

    Dim imbalance() As Double
    ReDim imbalance(0 To total - 1)

    Dim forced As Double
    For e = 0 To nEdge - 1
        If edgeCost(e) < 0 Then
            forced = edgeHi(e)
            edgeArc(e) = AddArc(edgeV(e), edgeU(e), edgeHi(e) - edgeLo(e), -edgeCost(e))
            edgeSign(e) = -1
            edgeBase(e) = edgeHi(e)
        Else
            forced = edgeLo(e)
            edgeArc(e) = AddArc(edgeU(e), edgeV(e), edgeHi(e) - edgeLo(e), edgeCost(e))
            edgeSign(e) = 1
            edgeBase(e) = edgeLo(e)
        End If
        edgeCap0(e) = edgeHi(e) - edgeLo(e)
        imbalance(edgeV(e)) = imbalance(edgeV(e)) + forced
        imbalance(edgeU(e)) = imbalance(edgeU(e)) - forced
    Next e

    Dim required As Double
    required = 0
    For n = 0 To total - 1
        If imbalance(n) > 0 Then
            AddArc src, n, imbalance(n), 0
            required = required + imbalance(n)
        ElseIf imbalance(n) < 0 Then
            AddArc n, snk, -imbalance(n), 0
        End If
    Next n

    NetSolve = (MinCostFlow(src, snk, required, total) >= required)
End Function

Private Function NetFlow(ByVal e As Long) As Double
    NetFlow = edgeBase(e) + edgeSign(e) * (edgeCap0(e) - arcCap(edgeArc(e)))
End Function

' Successive shortest paths with potentials. Node counts here are small, so a
' dense scan beats a heap and keeps the code short.
Private Function MinCostFlow(ByVal src As Long, ByVal snk As Long, _
                             ByVal required As Double, ByVal total As Long) As Double
    Dim pot() As Double, dist() As Double, came() As Long, settled() As Boolean
    ReDim pot(0 To total - 1)
    ReDim dist(0 To total - 1)
    ReDim came(0 To total - 1)
    ReDim settled(0 To total - 1)

    Dim sent As Double, push As Double
    Dim i As Long, node As Long, best As Long, a As Long
    Dim bestDist As Double, weight As Double

    sent = 0
    Do While sent < required
        For i = 0 To total - 1
            dist(i) = FR_INF
            came(i) = -1
            settled(i) = False
        Next i
        dist(src) = 0

        Do
            best = -1
            bestDist = FR_INF
            For i = 0 To total - 1
                If Not settled(i) Then
                    If dist(i) < bestDist Then
                        bestDist = dist(i)
                        best = i
                    End If
                End If
            Next i
            If best = -1 Then Exit Do
            settled(best) = True
            a = arcHead(best)
            Do While a <> -1
                If arcCap(a) > 0 Then
                    weight = arcCost(a) + pot(best) - pot(arcTo(a))
                    If Not settled(arcTo(a)) Then
                        If dist(best) + weight < dist(arcTo(a)) Then
                            dist(arcTo(a)) = dist(best) + weight
                            came(arcTo(a)) = a
                        End If
                    End If
                End If
                a = arcNext(a)
            Loop
        Loop

        If dist(snk) >= FR_INF Then Exit Do
        For i = 0 To total - 1
            If dist(i) < FR_INF Then pot(i) = pot(i) + dist(i)
        Next i

        push = required - sent
        node = snk
        Do While node <> src
            a = came(node)
            If arcCap(a) < push Then push = arcCap(a)
            node = arcTo(a Xor 1)
        Loop

        node = snk
        Do While node <> src
            a = came(node)
            arcCap(a) = arcCap(a) - push
            arcCap(a Xor 1) = arcCap(a Xor 1) + push
            node = arcTo(a Xor 1)
        Loop
        sent = sent + push
    Loop

    MinCostFlow = sent
End Function

'==============================================================================
' Ticks and whole steps
'==============================================================================

' A figure as a whole number of ticks. Double holds whole numbers exactly to
' 2^53, so every rounding decision below is made in exact integer arithmetic.
Private Function TicksOf(ByVal v As Double, ByVal scaleBy As Double, _
                         ByVal stepSize As Double) As Double
    TicksOf = Int(v / (scaleBy * stepSize) * FR_TICKS + 0.5)
End Function

Private Function FloorUnit(ByVal t As Double) As Double
    FloorUnit = Int(t / FR_TICKS)
End Function

Private Function CeilUnit(ByVal t As Double) As Double
    CeilUnit = -Int(-t / FR_TICKS)
End Function

' Nearest whole step, halves away from zero - the accounting convention.
Public Function FRNearestUnit(ByVal t As Double) As Double
    Dim f As Double, rest As Double
    f = FloorUnit(t)
    rest = t - f * FR_TICKS
    If rest * 2 > FR_TICKS Then
        FRNearestUnit = f + 1
    ElseIf rest * 2 < FR_TICKS Then
        FRNearestUnit = f
    ElseIf t > 0 Then
        FRNearestUnit = f + 1
    Else
        FRNearestUnit = f
    End If
End Function

' These fill a span in place rather than returning one. A function that
' returns a user-defined type cannot be assigned straight into an array
' element in every Basic dialect - it silently stores zeros - so spans are
' always written through a ByRef parameter.

' The quantity is already settled by an outer block.
Private Sub SetFixed(s As FRSpan, ByVal value As Double)
    s.Lo = value
    s.Hi = value
    s.Target = 0
    s.Costed = False
End Sub

' Anything in range will do: the quantity is not a figure the table prints,
' so only the constraints it carries matter.
Private Sub SetFree(s As FRSpan, ByVal lo As Double, ByVal hi As Double)
    s.Lo = lo
    s.Hi = hi
    s.Target = 0
    s.Costed = False
End Sub

' A printed figure: it must land on one of the two steps either side of its
' true value, and moving further from that value costs more.
Private Sub SetAdjacent(s As FRSpan, ByVal target As Double, ByVal slack As Long)
    s.Lo = FloorUnit(target) - slack
    s.Hi = CeilUnit(target) + slack
    s.Target = target
    s.Costed = True
End Sub

'==============================================================================
' One rectangular block
'==============================================================================

' A costed span becomes one unit-capacity arc per step, each priced at the
' extra distance that step costs. The prices increase, so the solver spends
' cheap steps first and the piecewise cost stays convex - which is what lets a
' linear network minimise an absolute deviation.
'
' eps prices a second, far smaller preference for the plain nearest value. It
' settles ties - a figure ending in exactly half, or a choice between two
' equally good cells - the way an accountant would. Every primary cost is a
' whole number of ticks, so keeping the total tie-break weight below one tick
' leaves the ranking of genuinely different roundings untouched.
Private Function AddSpan(ByVal u As Long, ByVal v As Long, s As FRSpan, _
                         ByVal eps As Double, ByRef count As Long) As Long
    Dim first As Long, x As Double, c As Double, plain As Double
    first = NetAddEdge(u, v, s.Lo, s.Lo, 0)
    count = 1
    If Not s.Costed Then
        NetAddEdge u, v, 0, s.Hi - s.Lo, 0
        count = 2
    Else
        plain = FRNearestUnit(s.Target)
        For x = s.Lo To s.Hi - 1
            c = Abs((x + 1) * FR_TICKS - s.Target) - Abs(x * FR_TICKS - s.Target)
            c = c + eps * (Abs(x + 1 - plain) - Abs(x - plain))
            NetAddEdge u, v, 0, 1, c
            count = count + 1
        Next x
    End If
    AddSpan = first
End Function

Private Function ReadSpan(ByVal first As Long, ByVal count As Long) As Double
    Dim k As Long, sum As Double
    For k = first To first + count - 1
        sum = sum + NetFlow(k)
    Next k
    ReadSpan = sum
End Function

Private Function SolveBlock(cells() As FRSpan, rowsS() As FRSpan, colsS() As FRSpan, _
                            corner As FRSpan, outCells() As Double, _
                            outRows() As Double, outCols() As Double, _
                            ByRef outCorner As Double) As Boolean
    Dim nR As Long, nC As Long, i As Long, j As Long
    nR = UBound(rowsS) + 1
    nC = UBound(colsS) + 1

    Dim steps As Double
    For i = 0 To nR - 1
        If rowsS(i).Costed Then steps = steps + rowsS(i).Hi - rowsS(i).Lo
        For j = 0 To nC - 1
            If cells(i, j).Costed Then steps = steps + cells(i, j).Hi - cells(i, j).Lo
        Next j
    Next i
    For j = 0 To nC - 1
        If colsS(j).Costed Then steps = steps + colsS(j).Hi - colsS(j).Lo
    Next j
    If corner.Costed Then steps = steps + corner.Hi - corner.Lo
    Dim eps As Double
    eps = 1 / (2 * steps + 1)

    Dim src As Long, snk As Long
    src = nR + nC
    snk = src + 1
    NetInit nR + nC + 2

    Dim rowFirst() As Long, rowCount() As Long
    Dim colFirst() As Long, colCount() As Long
    Dim cellFirst() As Long, cellCount() As Long
    Dim cornerFirst As Long, cornerCount As Long
    ReDim rowFirst(0 To nR - 1)
    ReDim rowCount(0 To nR - 1)
    ReDim colFirst(0 To nC - 1)
    ReDim colCount(0 To nC - 1)
    ReDim cellFirst(0 To nR - 1, 0 To nC - 1)
    ReDim cellCount(0 To nR - 1, 0 To nC - 1)

    For i = 0 To nR - 1
        rowFirst(i) = AddSpan(src, i, rowsS(i), eps, rowCount(i))
    Next i
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            cellFirst(i, j) = AddSpan(i, nR + j, cells(i, j), eps, cellCount(i, j))
        Next j
    Next i
    For j = 0 To nC - 1
        colFirst(j) = AddSpan(nR + j, snk, colsS(j), eps, colCount(j))
    Next j
    cornerFirst = AddSpan(snk, src, corner, eps, cornerCount)

    If Not NetSolve() Then
        SolveBlock = False
        Exit Function
    End If

    ReDim outCells(0 To nR - 1, 0 To nC - 1)
    ReDim outRows(0 To nR - 1)
    ReDim outCols(0 To nC - 1)
    For i = 0 To nR - 1
        outRows(i) = ReadSpan(rowFirst(i), rowCount(i))
        For j = 0 To nC - 1
            outCells(i, j) = ReadSpan(cellFirst(i, j), cellCount(i, j))
        Next j
    Next i
    For j = 0 To nC - 1
        outCols(j) = ReadSpan(colFirst(j), colCount(j))
    Next j
    outCorner = ReadSpan(cornerFirst, cornerCount)
    SolveBlock = True
End Function

'==============================================================================
' Trees of totals
'==============================================================================

Private Sub BuildTree(ByVal n As Long, parent() As Long, firstChild() As Long, _
                      nextSib() As Long, lastChild() As Long, depth() As Long)
    Dim i As Long, p As Long, root As Long
    root = n
    ReDim firstChild(0 To n)
    ReDim nextSib(0 To n)
    ReDim lastChild(0 To n)
    ReDim depth(0 To n)
    For i = 0 To n
        firstChild(i) = -1
        nextSib(i) = -1
        lastChild(i) = -1
    Next i
    For i = 0 To n - 1
        p = parent(i)
        If p < 0 Then p = root
        If firstChild(p) = -1 Then
            firstChild(p) = i
        Else
            nextSib(lastChild(p)) = i
        End If
        lastChild(p) = i
    Next i

    Dim stack() As Long, sp As Long, node As Long, k As Long
    ReDim stack(0 To n + 1)
    depth(root) = 0
    stack(0) = root
    sp = 1
    Do While sp > 0
        sp = sp - 1
        node = stack(sp)
        k = firstChild(node)
        Do While k <> -1
            depth(k) = depth(node) + 1
            stack(sp) = k
            sp = sp + 1
            k = nextSib(k)
        Loop
    Loop
End Sub

Private Function ChildrenOf(firstChild() As Long, nextSib() As Long, _
                            ByVal node As Long, outList() As Long) As Long
    Dim n As Long, k As Long
    k = firstChild(node)
    Do While k <> -1
        n = n + 1
        k = nextSib(k)
    Loop
    If n = 0 Then Exit Function
    ReDim outList(0 To n - 1)
    n = 0
    k = firstChild(node)
    Do While k <> -1
        outList(n) = k
        n = n + 1
        k = nextSib(k)
    Loop
    ChildrenOf = n
End Function

'==============================================================================
' Detecting which rows and columns are totals
'==============================================================================

Public Function FRIsTotalLabel(ByVal text As String) As Boolean
    Dim t As String
    t = LCase$(Trim$(text))
    If Len(t) = 0 Then Exit Function
    FRIsTotalLabel = (InStr(t, "total") > 0) Or (InStr(t, "aggregate") > 0) _
                     Or (InStr(t, "sum of") > 0)
End Function

Private Function CloseEnough(ByVal a As Double, ByVal b As Double, _
                             ByVal tol As Double) As Boolean
    CloseEnough = (Abs(a - b) <= tol + FR_REL_EPS * (1 + Abs(b)))
End Function

' For an item that reads like a total, take the longest trailing block of
' preceding items that adds up to it IN EVERY COLUMN AT ONCE - agreement
' across all of them makes a coincidental match very unlikely. A matched block
' rolls up into the total, so an outer total sums subtotals rather than raw
' lines and nesting works to any depth. An item that nothing adds up to is
' reported, never invented.
Private Function DetectAxis(vecs() As Double, ByVal n As Long, ByVal m As Long, _
                            labels() As String, ByVal useLabels As Boolean, _
                            candidate() As Boolean, ByVal tol As Double, _
                            parent() As Long, unreconciled() As Boolean) As Long
    Dim pending() As Long, nPending As Long
    Dim acc() As Double
    Dim i As Long, j As Long, k As Long, size As Long, bestSize As Long
    Dim isCandidate As Boolean, ok As Boolean, found As Long

    ReDim pending(0 To n)
    ReDim acc(0 To m - 1)
    For i = 0 To n - 1
        parent(i) = -1
        unreconciled(i) = False
    Next i

    For i = 0 To n - 1
        If useLabels Then
            isCandidate = FRIsTotalLabel(labels(i))
        Else
            isCandidate = candidate(i)
        End If

        bestSize = 0
        If isCandidate And nPending > 0 Then
            For j = 0 To m - 1
                acc(j) = 0
            Next j
            For size = 1 To nPending
                k = pending(nPending - size)
                For j = 0 To m - 1
                    acc(j) = acc(j) + vecs(k, j)
                Next j
                ok = True
                For j = 0 To m - 1
                    If Not CloseEnough(acc(j), vecs(i, j), tol) Then
                        ok = False
                        Exit For
                    End If
                Next j
                If ok Then bestSize = size
            Next size
        End If

        If bestSize > 0 Then
            For k = nPending - bestSize To nPending - 1
                parent(pending(k)) = i
            Next k
            nPending = nPending - bestSize
            found = found + 1
        ElseIf isCandidate Then
            unreconciled(i) = True
        End If
        pending(nPending) = i
        nPending = nPending + 1
    Next i
    DetectAxis = found
End Function

' Work out the structure of a table. rowParent(i) receives the row that claims
' row i as a component, or -1. Pass useRowLabels = False and rowCandidate() to
' name the total rows yourself. Columns likewise.
Public Function FRDetect(vals() As Double, ByVal nR As Long, ByVal nC As Long, _
                         rowLabels() As String, colLabels() As String, _
                         ByVal useRowLabels As Boolean, ByVal useColLabels As Boolean, _
                         rowCandidate() As Boolean, colCandidate() As Boolean, _
                         ByVal tol As Double, rowParent() As Long, colParent() As Long, _
                         rowBad() As Boolean, colBad() As Boolean) As Long
    Dim found As Long, i As Long, j As Long
    ReDim rowParent(0 To nR - 1)
    ReDim colParent(0 To nC - 1)
    ReDim rowBad(0 To nR - 1)
    ReDim colBad(0 To nC - 1)

    found = DetectAxis(vals, nR, nC, rowLabels, useRowLabels, rowCandidate, tol, _
                       rowParent, rowBad)

    Dim flipped() As Double
    ReDim flipped(0 To nC - 1, 0 To nR - 1)
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            flipped(j, i) = vals(i, j)
        Next j
    Next i
    found = found + DetectAxis(flipped, nC, nR, colLabels, useColLabels, colCandidate, _
                               tol, colParent, colBad)
    FRDetect = found
End Function

'==============================================================================
' The cascade
'==============================================================================

' Round every figure of the table. outUnits(i, j) receives the answer as a
' whole number of steps, so the figure to print is outUnits(i, j) * stepSize.
' Returns False only if the table itself is malformed.
Public Function FRRound(vals() As Double, ByVal nR As Long, ByVal nC As Long, _
                        rowParent() As Long, colParent() As Long, _
                        ByVal scaleBy As Double, ByVal stepSize As Double, _
                        outUnits() As Double) As Boolean
    FRLastSlack = 0
    FRLastMessage = ""
    If nR < 1 Or nC < 1 Then
        FRLastMessage = "the table has no figures"
        Exit Function
    End If
    If scaleBy = 0 Or stepSize <= 0 Then
        FRLastMessage = "the scale must not be zero and the step must be positive"
        Exit Function
    End If

    Dim ticks() As Double
    Dim i As Long, j As Long, biggest As Double
    ReDim ticks(0 To nR - 1, 0 To nC - 1)
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            ticks(i, j) = TicksOf(vals(i, j), scaleBy, stepSize)
            If Abs(ticks(i, j)) > biggest Then biggest = Abs(ticks(i, j))
        Next j
    Next i
    ' Whole-number arithmetic is only exact below 2^53; a block sums every one
    ' of its cells, so refuse the job rather than return quietly wrong figures.
    If biggest * nR * nC > 4E+15 Then
        FRLastMessage = "these figures are too large to round exactly at this step; " & _
            "divide them down with a scale (thousands, lakh, crore) first"
        Exit Function
    End If

    Dim rFirst() As Long, rNext() As Long, rLast() As Long, rDepth() As Long
    Dim cFirst() As Long, cNext() As Long, cLast() As Long, cDepth() As Long
    BuildTree nR, rowParent, rFirst, rNext, rLast, rDepth
    BuildTree nC, colParent, cFirst, cNext, cLast, cDepth

    ' True value of every pair of nodes, in ticks. Only the virtual roots -
    ' node nR and node nC, standing for "everything" - are not table cells.
    Dim tt() As Double
    ReDim tt(0 To nR, 0 To nC)
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            tt(i, j) = ticks(i, j)
        Next j
    Next i
    Dim topRows() As Long, topCols() As Long, nTopR As Long, nTopC As Long, k As Long
    nTopR = ChildrenOf(rFirst, rNext, nR, topRows)
    nTopC = ChildrenOf(cFirst, cNext, nC, topCols)
    For i = 0 To nR - 1
        tt(i, nC) = 0
        For k = 0 To nTopC - 1
            tt(i, nC) = tt(i, nC) + tt(i, topCols(k))
        Next k
    Next i
    For j = 0 To nC
        tt(nR, j) = 0
        For k = 0 To nTopR - 1
            tt(nR, j) = tt(nR, j) + tt(topRows(k), j)
        Next k
    Next j

    Dim slack As Long
    For slack = 0 To FR_MAX_SLACK
        If RunCascade(tt, nR, nC, rFirst, rNext, rDepth, cFirst, cNext, cDepth, _
                      slack, outUnits) Then
            FRLastSlack = slack
            If slack > 0 Then
                FRLastMessage = "no rounding within one step of every figure could " & _
                    "satisfy this structure; " & slack & " extra step(s) of leeway were used"
            End If
            FRRound = True
            Exit Function
        End If
    Next slack
    FRLastMessage = "no consistent rounding of this table exists"
End Function

' Blocks are solved in order of depth, outermost first. Whatever an outer
' block decided is fixed when the block that refines it is solved, so every
' figure is decided exactly once and consistency holds by construction.
Private Function RunCascade(tt() As Double, ByVal nR As Long, ByVal nC As Long, _
                            rFirst() As Long, rNext() As Long, rDepth() As Long, _
                            cFirst() As Long, cNext() As Long, cDepth() As Long, _
                            ByVal slack As Long, outUnits() As Double) As Boolean
    Dim decided() As Double, known() As Boolean
    ReDim decided(0 To nR, 0 To nC)
    ReDim known(0 To nR, 0 To nC)

    Dim rInner() As Long, cInner() As Long, nRI As Long, nCI As Long
    Dim n As Long, maxSum As Long, depthSum As Long
    ReDim rInner(0 To nR)
    ReDim cInner(0 To nC)
    For n = 0 To nR
        If rFirst(n) <> -1 Then
            rInner(nRI) = n
            nRI = nRI + 1
            If rDepth(n) > maxSum Then maxSum = rDepth(n)
        End If
    Next n
    Dim maxC As Long
    For n = 0 To nC
        If cFirst(n) <> -1 Then
            cInner(nCI) = n
            nCI = nCI + 1
            If cDepth(n) > maxC Then maxC = cDepth(n)
        End If
    Next n
    maxSum = maxSum + maxC

    Dim a As Long, b As Long
    For depthSum = 0 To maxSum
        For a = 0 To nRI - 1
            For b = 0 To nCI - 1
                If rDepth(rInner(a)) + cDepth(cInner(b)) = depthSum Then
                    If Not SolveOneBlock(tt, nR, nC, rInner(a), cInner(b), _
                                         rFirst, rNext, cFirst, cNext, _
                                         slack, decided, known) Then
                        Exit Function
                    End If
                End If
            Next b
        Next a
    Next depthSum

    Dim i As Long, j As Long
    ReDim outUnits(0 To nR - 1, 0 To nC - 1)
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            If Not known(i, j) Then Exit Function
            outUnits(i, j) = decided(i, j)
        Next j
    Next i
    RunCascade = True
End Function

Private Function SolveOneBlock(tt() As Double, ByVal nR As Long, ByVal nC As Long, _
                               ByVal rn As Long, ByVal cn As Long, _
                               rFirst() As Long, rNext() As Long, _
                               cFirst() As Long, cNext() As Long, _
                               ByVal slack As Long, decided() As Double, _
                               known() As Boolean) As Boolean
    Dim rowsOf() As Long, colsOf() As Long, nRB As Long, nCB As Long
    nRB = ChildrenOf(rFirst, rNext, rn, rowsOf)
    nCB = ChildrenOf(cFirst, cNext, cn, colsOf)
    If nRB = 0 Or nCB = 0 Then
        SolveOneBlock = True
        Exit Function
    End If

    Dim cells() As FRSpan, rowsS() As FRSpan, colsS() As FRSpan, corner As FRSpan
    ReDim cells(0 To nRB - 1, 0 To nCB - 1)
    ReDim rowsS(0 To nRB - 1)
    ReDim colsS(0 To nCB - 1)

    Dim i As Long, j As Long, lo As Double, hi As Double
    Dim allLo As Double, allHi As Double
    For i = 0 To nRB - 1
        For j = 0 To nCB - 1
            SetAdjacent cells(i, j), tt(rowsOf(i), colsOf(j)), slack
            allLo = allLo + cells(i, j).Lo
            allHi = allHi + cells(i, j).Hi
        Next j
    Next i

    For i = 0 To nRB - 1
        If known(rowsOf(i), cn) Then
            SetFixed rowsS(i), decided(rowsOf(i), cn)
        ElseIf rowsOf(i) < nR And cn < nC Then
            SetAdjacent rowsS(i), tt(rowsOf(i), cn), slack
        Else
            lo = 0
            hi = 0
            For j = 0 To nCB - 1
                lo = lo + cells(i, j).Lo
                hi = hi + cells(i, j).Hi
            Next j
            SetFree rowsS(i), lo, hi
        End If
    Next i

    For j = 0 To nCB - 1
        If known(rn, colsOf(j)) Then
            SetFixed colsS(j), decided(rn, colsOf(j))
        ElseIf rn < nR And colsOf(j) < nC Then
            SetAdjacent colsS(j), tt(rn, colsOf(j)), slack
        Else
            lo = 0
            hi = 0
            For i = 0 To nRB - 1
                lo = lo + cells(i, j).Lo
                hi = hi + cells(i, j).Hi
            Next i
            SetFree colsS(j), lo, hi
        End If
    Next j

    If known(rn, cn) Then
        SetFixed corner, decided(rn, cn)
    ElseIf rn < nR And cn < nC Then
        SetAdjacent corner, tt(rn, cn), slack
    Else
        SetFree corner, allLo, allHi
    End If

    Dim outCells() As Double, outRows() As Double, outCols() As Double, outCorner As Double
    If Not SolveBlock(cells, rowsS, colsS, corner, outCells, outRows, outCols, outCorner) Then
        Exit Function
    End If

    For i = 0 To nRB - 1
        For j = 0 To nCB - 1
            decided(rowsOf(i), colsOf(j)) = outCells(i, j)
            known(rowsOf(i), colsOf(j)) = True
        Next j
        decided(rowsOf(i), cn) = outRows(i)
        known(rowsOf(i), cn) = True
    Next i
    For j = 0 To nCB - 1
        decided(rn, colsOf(j)) = outCols(j)
        known(rn, colsOf(j)) = True
    Next j
    decided(rn, cn) = outCorner
    known(rn, cn) = True
    SolveOneBlock = True
End Function

'==============================================================================
' Checking the answer, and describing it
'==============================================================================

' Rounding each figure on its own - what the table would look like without any
' of this, and what the report compares against.
Public Sub FRNaive(vals() As Double, ByVal nR As Long, ByVal nC As Long, _
                   ByVal scaleBy As Double, ByVal stepSize As Double, _
                   outUnits() As Double)
    Dim i As Long, j As Long
    ReDim outUnits(0 To nR - 1, 0 To nC - 1)
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            outUnits(i, j) = FRNearestUnit(TicksOf(vals(i, j), scaleBy, stepSize))
        Next j
    Next i
End Sub

' Re-derive every total from the printed figures. The answer is only worth
' anything if this returns 0.
Public Function FRViolations(units() As Double, ByVal nR As Long, ByVal nC As Long, _
                             rowParent() As Long, colParent() As Long) As Long
    Dim i As Long, j As Long, k As Long, sum As Double, members As Long, bad As Long
    For i = 0 To nR - 1
        members = 0
        For k = 0 To nR - 1
            If rowParent(k) = i Then members = members + 1
        Next k
        If members > 0 Then
            For j = 0 To nC - 1
                sum = 0
                For k = 0 To nR - 1
                    If rowParent(k) = i Then sum = sum + units(k, j)
                Next k
                If sum <> units(i, j) Then bad = bad + 1
            Next j
        End If
    Next i
    For j = 0 To nC - 1
        members = 0
        For k = 0 To nC - 1
            If colParent(k) = j Then members = members + 1
        Next k
        If members > 0 Then
            For i = 0 To nR - 1
                sum = 0
                For k = 0 To nC - 1
                    If colParent(k) = j Then sum = sum + units(i, k)
                Next k
                If sum <> units(i, j) Then bad = bad + 1
            Next i
        End If
    Next j
    FRViolations = bad
End Function

Public Function FRCheckCount(ByVal nR As Long, ByVal nC As Long, _
                             rowParent() As Long, colParent() As Long) As Long
    Dim i As Long, k As Long, totals As Long, n As Long
    For i = 0 To nR - 1
        For k = 0 To nR - 1
            If rowParent(k) = i Then
                totals = totals + 1
                Exit For
            End If
        Next k
    Next i
    n = totals * nC
    totals = 0
    For i = 0 To nC - 1
        For k = 0 To nC - 1
            If colParent(k) = i Then
                totals = totals + 1
                Exit For
            End If
        Next k
    Next i
    FRCheckCount = n + totals * nR
End Function

' Total distance every figure moved, counted in rounding steps.
Public Function FRMovement(vals() As Double, ByVal nR As Long, ByVal nC As Long, _
                           ByVal scaleBy As Double, ByVal stepSize As Double, _
                           units() As Double) As Double
    Dim i As Long, j As Long, sum As Double, t As Double
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            t = TicksOf(vals(i, j), scaleBy, stepSize)
            sum = sum + Abs(units(i, j) * FR_TICKS - t) / FR_TICKS
        Next j
    Next i
    FRMovement = sum
End Function

Public Function FRWorstMove(vals() As Double, ByVal nR As Long, ByVal nC As Long, _
                            ByVal scaleBy As Double, ByVal stepSize As Double, _
                            units() As Double) As Double
    Dim i As Long, j As Long, worst As Double, t As Double, d As Double
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            t = TicksOf(vals(i, j), scaleBy, stepSize)
            d = Abs(units(i, j) * FR_TICKS - t) / FR_TICKS
            If d > worst Then worst = d
        Next j
    Next i
    FRWorstMove = worst
End Function

'==============================================================================
' Self test - run this first, it needs no worksheet
'==============================================================================

Public Function FRSelfTest() As String
    Dim report As String, failures As Long

    report = report & Check1(failures) & Chr$(10)
    report = report & Check2(failures) & Chr$(10)
    report = report & Check3(failures) & Chr$(10)
    report = report & Check4(failures) & Chr$(10)
    report = report & Check5(failures) & Chr$(10)
    If failures = 0 Then
        report = report & "ALL PASSED"
    Else
        report = report & failures & " CHECK(S) FAILED"
    End If
    FRSelfTest = report
End Function

Private Function Check1(ByRef failures As Long) As String
    ' The worked example: every one of the 8 totals breaks under plain rounding.
    Dim vals() As Double, units() As Double
    Dim rowParent() As Long, colParent() As Long
    Dim rowLabels() As String, colLabels() As String
    Dim rowCand() As Boolean, colCand() As Boolean
    Dim rowBad() As Boolean, colBad() As Boolean
    Dim i As Long, j As Long

    ReDim vals(0 To 3, 0 To 3)
    vals(0, 0) = 10.4: vals(0, 1) = 20.4: vals(0, 2) = 30.4: vals(0, 3) = 61.2
    vals(1, 0) = 40.4: vals(1, 1) = 50.4: vals(1, 2) = 60.4: vals(1, 3) = 151.2
    vals(2, 0) = 70.4: vals(2, 1) = 80.4: vals(2, 2) = 90.4: vals(2, 3) = 241.2
    vals(3, 0) = 121.2: vals(3, 1) = 151.2: vals(3, 2) = 181.2: vals(3, 3) = 453.6
    ReDim rowLabels(0 To 3)
    ReDim colLabels(0 To 3)
    rowLabels(0) = "North": rowLabels(1) = "South": rowLabels(2) = "East": rowLabels(3) = "Total"
    colLabels(0) = "Q1": colLabels(1) = "Q2": colLabels(2) = "Q3": colLabels(3) = "Total"
    ReDim rowCand(0 To 3)
    ReDim colCand(0 To 3)

    FRDetect vals, 4, 4, rowLabels, colLabels, True, True, rowCand, colCand, 0, _
             rowParent, colParent, rowBad, colBad

    Dim ok As Boolean
    ok = (rowParent(0) = 3 And rowParent(1) = 3 And rowParent(2) = 3 And rowParent(3) = -1)
    ok = ok And (colParent(0) = 3 And colParent(1) = 3 And colParent(2) = 3)
    If Not ok Then
        failures = failures + 1
        Check1 = "FAIL structure: " & rowParent(0) & rowParent(1) & rowParent(2) & rowParent(3)
        Exit Function
    End If

    If Not FRRound(vals, 4, 4, rowParent, colParent, 1, 1, units) Then
        failures = failures + 1
        Check1 = "FAIL round: " & FRLastMessage
        Exit Function
    End If

    Dim bad As Long
    bad = FRViolations(units, 4, 4, rowParent, colParent)
    Dim worst As Double
    worst = FRWorstMove(vals, 4, 4, 1, 1, units)
    If bad > 0 Or worst >= 1 Or units(3, 3) <> 454 Then
        failures = failures + 1
        Check1 = "FAIL 4x4: violations=" & bad & " worst=" & worst & " grand=" & units(3, 3)
        Exit Function
    End If
    Check1 = "ok  4x4 worked example: 8/8 totals foot, worst move " & worst & _
             ", grand total " & units(3, 3)
End Function

Private Function Check2(ByRef failures As Long) As String
    ' Thirds: rows and columns of 33.3 that must both reach 100.
    Dim vals() As Double, units() As Double
    Dim rowParent() As Long, colParent() As Long
    Dim i As Long, j As Long
    ReDim vals(0 To 3, 0 To 3)
    vals(0, 0) = 33.3: vals(0, 1) = 33.3: vals(0, 2) = 33.4: vals(0, 3) = 100
    vals(1, 0) = 33.3: vals(1, 1) = 33.3: vals(1, 2) = 33.4: vals(1, 3) = 100
    vals(2, 0) = 33.4: vals(2, 1) = 33.4: vals(2, 2) = 33.2: vals(2, 3) = 100
    vals(3, 0) = 100: vals(3, 1) = 100: vals(3, 2) = 100: vals(3, 3) = 300
    ReDim rowParent(0 To 3)
    ReDim colParent(0 To 3)
    For i = 0 To 2
        rowParent(i) = 3
        colParent(i) = 3
    Next i
    rowParent(3) = -1
    colParent(3) = -1

    If Not FRRound(vals, 4, 4, rowParent, colParent, 1, 1, units) Then
        failures = failures + 1
        Check2 = "FAIL thirds: " & FRLastMessage
        Exit Function
    End If
    Dim bad As Long
    bad = FRViolations(units, 4, 4, rowParent, colParent)
    If bad > 0 Or units(3, 3) <> 300 Then
        failures = failures + 1
        Check2 = "FAIL thirds: violations=" & bad
        Exit Function
    End If
    Check2 = "ok  thirds: every row and column reaches 100, grand total " & units(3, 3)
End Function

Private Function Check3(ByRef failures As Long) As String
    ' Nested subtotals, in rupees presented as thousands to one decimal.
    Dim vals() As Double, units() As Double
    Dim rowParent() As Long, colParent() As Long
    Dim rowLabels() As String, colLabels() As String
    Dim rowCand() As Boolean, colCand() As Boolean
    Dim rowBad() As Boolean, colBad() As Boolean
    Dim i As Long

    ReDim vals(0 To 6, 0 To 2)
    vals(0, 0) = 1234567: vals(0, 1) = 2345678
    vals(1, 0) = 3456789: vals(1, 1) = 1111111
    vals(2, 0) = vals(0, 0) + vals(1, 0): vals(2, 1) = vals(0, 1) + vals(1, 1)
    vals(3, 0) = 7777777: vals(3, 1) = 8888888
    vals(4, 0) = 999999: vals(4, 1) = 1010101
    vals(5, 0) = vals(3, 0) + vals(4, 0): vals(5, 1) = vals(3, 1) + vals(4, 1)
    vals(6, 0) = vals(2, 0) + vals(5, 0): vals(6, 1) = vals(2, 1) + vals(5, 1)
    For i = 0 To 6
        vals(i, 2) = vals(i, 0) + vals(i, 1)
    Next i

    ReDim rowLabels(0 To 6)
    ReDim colLabels(0 To 2)
    rowLabels(0) = "Cash": rowLabels(1) = "Receivables"
    rowLabels(2) = "Total current assets"
    rowLabels(3) = "Property, plant and equipment": rowLabels(4) = "Goodwill"
    rowLabels(5) = "Total non-current assets"
    rowLabels(6) = "Total assets"
    colLabels(0) = "FY24": colLabels(1) = "FY25": colLabels(2) = "Total"
    ReDim rowCand(0 To 6)
    ReDim colCand(0 To 2)

    FRDetect vals, 7, 3, rowLabels, colLabels, True, True, rowCand, colCand, 0, _
             rowParent, colParent, rowBad, colBad
    If rowParent(0) <> 2 Or rowParent(2) <> 6 Or rowParent(5) <> 6 Or colParent(0) <> 2 Then
        failures = failures + 1
        Check3 = "FAIL nested structure"
        Exit Function
    End If

    If Not FRRound(vals, 7, 3, rowParent, colParent, 1000, 0.1, units) Then
        failures = failures + 1
        Check3 = "FAIL nested: " & FRLastMessage
        Exit Function
    End If
    Dim bad As Long, worst As Double
    bad = FRViolations(units, 7, 3, rowParent, colParent)
    worst = FRWorstMove(vals, 7, 3, 1000, 0.1, units)
    If bad > 0 Or worst >= 1 Then
        failures = failures + 1
        Check3 = "FAIL nested: violations=" & bad & " worst=" & worst
        Exit Function
    End If
    Check3 = "ok  nested subtotals in thousands: " & _
             FRCheckCount(7, 3, rowParent, colParent) & " checks foot, worst move " & worst
End Function

Private Function Check4(ByRef failures As Long) As String
    ' Negatives, and a total that is more negative than any of its parts.
    Dim vals() As Double, units() As Double
    Dim rowParent() As Long, colParent() As Long
    ReDim vals(0 To 3, 0 To 0)
    vals(0, 0) = -10.5: vals(1, 0) = -10.5: vals(2, 0) = -10.5: vals(3, 0) = -31.5
    ReDim rowParent(0 To 3)
    ReDim colParent(0 To 0)
    rowParent(0) = 3: rowParent(1) = 3: rowParent(2) = 3: rowParent(3) = -1
    colParent(0) = -1

    If Not FRRound(vals, 4, 1, rowParent, colParent, 1, 1, units) Then
        failures = failures + 1
        Check4 = "FAIL negatives: " & FRLastMessage
        Exit Function
    End If
    If FRViolations(units, 4, 1, rowParent, colParent) > 0 Or units(3, 0) <> -32 Then
        failures = failures + 1
        Check4 = "FAIL negatives: total=" & units(3, 0)
        Exit Function
    End If
    Check4 = "ok  negatives: " & units(0, 0) & " + " & units(1, 0) & " + " & _
             units(2, 0) & " = " & units(3, 0)
End Function

Private Function Check5(ByRef failures As Long) As String
    ' A pile of awkward numbers, to show it holds up at size.
    Dim vals() As Double, units() As Double
    Dim rowParent() As Long, colParent() As Long
    Dim nR As Long, nC As Long, i As Long, j As Long, seed As Double
    nR = 13
    nC = 8
    ReDim vals(0 To nR - 1, 0 To nC - 1)
    seed = 12345
    For i = 0 To nR - 2
        For j = 0 To nC - 2
            seed = seed * 16807
            seed = seed - Int(seed / 2147483647#) * 2147483647#
            vals(i, j) = Int(seed / 2147483647# * 900000) / 100 - 1000
        Next j
    Next i
    For i = 0 To nR - 2
        vals(i, nC - 1) = 0
        For j = 0 To nC - 2
            vals(i, nC - 1) = vals(i, nC - 1) + vals(i, j)
        Next j
    Next i
    For j = 0 To nC - 1
        vals(nR - 1, j) = 0
        For i = 0 To nR - 2
            vals(nR - 1, j) = vals(nR - 1, j) + vals(i, j)
        Next i
    Next j

    ReDim rowParent(0 To nR - 1)
    ReDim colParent(0 To nC - 1)
    For i = 0 To nR - 2
        rowParent(i) = nR - 1
    Next i
    rowParent(nR - 1) = -1
    For j = 0 To nC - 2
        colParent(j) = nC - 1
    Next j
    colParent(nC - 1) = -1

    If Not FRRound(vals, nR, nC, rowParent, colParent, 1, 1, units) Then
        failures = failures + 1
        Check5 = "FAIL 13x8: " & FRLastMessage
        Exit Function
    End If
    Dim bad As Long, worst As Double
    bad = FRViolations(units, nR, nC, rowParent, colParent)
    worst = FRWorstMove(vals, nR, nC, 1, 1, units)
    If bad > 0 Or worst >= 1 Or FRLastSlack <> 0 Then
        failures = failures + 1
        Check5 = "FAIL 13x8: violations=" & bad & " worst=" & worst
        Exit Function
    End If
    Check5 = "ok  13x8 of awkward figures: " & _
             FRCheckCount(nR, nC, rowParent, colParent) & " checks foot, worst move " & worst
End Function
