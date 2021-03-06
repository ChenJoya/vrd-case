import json

import torch
from torch import nn
from torch.functional import F
from torchvision import models
from torchvision import transforms


from .backbone import build_backbone
from .detector import build_detector
from .encoder import build_encoder
from .tasks import ObjCls





def pack(lss):
    return [ls for ls in lss if len(ls) > 0]

def split(ds, lengths):
    segments = []
    begin = 0
    for i in lengths:
        end = begin + i
        segments.append(ds[begin:end])
        begin = end
    return segments


def topk_relations(probs, K):
    masks = torch.zeros_like(probs, 
        device=probs.device, dtype=torch.bool)
    masks_view = masks.view(-1)
    idxs = probs.view(-1).topk(K)[1]
    masks_view[idxs] = 1
    # K x 4
    nonzeros = masks.nonzero(as_tuple=False)
    return nonzeros[:,0], nonzeros[:,1:]

def reverse(mask):
    _, k, _ = mask.nonzero(as_tuple=True)
    mask = torch.zeros_like(mask, 
        device=mask.device, dtype=torch.bool)
    mask[:,k,:] = 1
    return ~mask

class Model(nn.Module):
    

    def init_catreldet(self, cfg):
        self.detector = build_detector(cfg, cfg.MODEL.NUM_OBJ_CLASSES, bce_roi_heads=True)
        hidden_channels = cfg.MODEL.HIDDEN_CHANNELS

        self.encoder = build_encoder("resnet34", hidden_channels)

        self.num_pred_classes = cfg.MODEL.NUM_PRED_CLASSES
        self.iou_floor = cfg.MODEL.IOU_FLOOR
        self.n2p = cfg.MODEL.N2P
        self.transform = transforms.Compose([
            transforms.Resize(cfg.INPUT.SIZE),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
            ),
        ])

        from vrd.data.datasets.vr import VR
        self.masks = VR.MASKS.cuda()
        
        self.bert_relations = torch.load("relations_without_comp.pth").cuda()
        
        self.unet = nn.Sequential(
            nn.Linear(hidden_channels*3, hidden_channels),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_channels, hidden_channels),
            nn.ReLU(inplace=True),
        )

        self.rnet = nn.Sequential(
            nn.Linear(hidden_channels*2, hidden_channels),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_channels, hidden_channels),
            nn.ReLU(inplace=True),
        )

        priors = json.load(open("priors.json"))
        self.rfc = nn.Linear(hidden_channels, cfg.MODEL.NUM_PRED_CLASSES)
        prior = torch.tensor(priors['all_predcls'])
        self.rfc.bias.data = -torch.log((1 - prior) / prior)

        self.num_obj_classes = cfg.MODEL.NUM_OBJ_CLASSES
    
    
    def match_and_sample(self, bboxes, sbboxes, scats, sidxs, ubboxes, obboxes, ocats, oidxs,
        gt_bboxes, gt_sbboxes, gt_scats, gt_sidxs, gt_ubboxes, gt_upreds, gt_obboxes, gt_ocats, gt_oidxs):
        # match strategy: 
        # (1) we match the union bboxes > 0.5, 
        # (2) we match if subject and object iou > 0.5
        # (2) we match if have correct subject and object category.
        # y1,y2,x1,x2 -> x1,y1,x2,y2
        labels = -torch.ones(
            len(ubboxes), 
            device=ubboxes.device, 
            dtype=torch.long
        )
        sious = box_iou(sbboxes[:, (2,0,3,1)], gt_sbboxes[:, (2,0,3,1)])
        uious = box_iou(ubboxes[:, (2,0,3,1)], gt_ubboxes[:, (2,0,3,1)])
        oious = box_iou(obboxes[:, (2,0,3,1)], gt_obboxes[:, (2,0,3,1)])
        nonzeros = (uious >= self.iou_floor).nonzero(as_tuple=False)
        
        for i, j in nonzeros:
            if sious[i, j] >= self.iou_floor and oious[i, j] >= self.iou_floor \
                and scats[i] == gt_scats[j] and ocats[i] == gt_ocats[j]:
                labels[i] = j
        
        pos_mask = labels >= 0
        if pos_mask.sum() > 0:
            pos_ubboxes = ubboxes[pos_mask]
            pos_upreds = gt_upreds[labels[pos_mask]]
            pos_sbboxes, pos_obboxes = sbboxes[pos_mask], obboxes[pos_mask]
            pos_scats, pos_ocats = scats[pos_mask], ocats[pos_mask]
            pos_sidxs, pos_oidxs = sidxs[pos_mask], oidxs[pos_mask]

            pos_ubboxes = torch.cat([pos_ubboxes, gt_ubboxes])
            pos_upreds = torch.cat([pos_upreds, gt_upreds])
            pos_sbboxes, pos_obboxes = torch.cat([pos_sbboxes, gt_sbboxes]), \
                torch.cat([pos_obboxes, gt_obboxes])
            pos_scats, pos_ocats = torch.cat([pos_scats, gt_scats]), \
                torch.cat([pos_ocats, gt_ocats])
            pos_sidxs, pos_oidxs = torch.cat([pos_sidxs, len(bboxes) + gt_sidxs]), \
                torch.cat([pos_oidxs, len(bboxes) + gt_oidxs])
            bboxes = torch.cat([bboxes, gt_bboxes])
        else:
            pos_ubboxes, pos_upreds = gt_ubboxes, gt_upreds
            pos_sbboxes, pos_obboxes = gt_sbboxes, gt_obboxes
            pos_scats, pos_ocats = gt_scats, gt_ocats
            pos_sidxs, pos_oidxs = len(bboxes) + gt_sidxs, len(bboxes) + gt_oidxs
            bboxes = torch.cat([bboxes, gt_bboxes])
        
        # produce negatives
        neg_mask = labels < 0
        if neg_mask.sum() > 0:
            num_neg = self.n2p * len(pos_ubboxes)
            neg_idxs = neg_mask.nonzero(as_tuple=False)
            print(neg_idxs.shape)
            neg_idxs = neg_idxs[torch.randperm(neg_idxs.numel(), device=neg_idxs.device)[:num_neg]].squeeze(1)
            neg_ubboxes = ubboxes[neg_idxs]
            neg_upreds = torch.zeros(len(neg_ubboxes), device=neg_ubboxes.device) + self.num_pred_classes
            neg_sbboxes, neg_obboxes = sbboxes[neg_idxs], obboxes[neg_idxs].view
            neg_scats, neg_ocats = scats[neg_idxs], ocats[neg_idxs]
            neg_sidxs, neg_oidxs = sidxs[neg_idxs], oidxs[neg_idxs]

        
        ubboxes = torch.cat([pos_ubboxes, neg_ubboxes])
        upreds = torch.cat([pos_upreds, neg_upreds])
        sbboxes, obboxes = torch.cat([pos_sbboxes, neg_sbboxes]), \
            torch.cat([pos_obboxes, neg_obboxes])
        scats, ocats = torch.cat([pos_scats, neg_scats]), \
            torch.cat([pos_ocats, neg_ocats])
        sidxs, oidxs = torch.cat([pos_sidxs, neg_sidxs]), \
            torch.cat([pos_oidxs, neg_oidxs])

        return bboxes, sbboxes, scats, sidxs, ubboxes, upreds, obboxes, ocats, oidxs

    def forward_reldet(self, batch):
        images, _, gt_relationships = batch

        with torch.no_grad():
            if self.detector.training:
                self.detector.eval()
            all_bboxes, all_probs = self.detector(images)
        
        relationships_50, relationships_100 = [], []
        predictions, targets = [], []
        for idx, (image, bboxes, bprobs) in \
            enumerate(zip(images, all_bboxes, all_probs)):
            
            if len(bboxes) < 2:
                continue
            
            image_idx = gt_relationships.idxs[idx]
            
            ubboxes, triplets = pairwise_ubboxes(bboxes)
            # we only care R@100 and R@50
            triplets = triplets[:100]
            sidxs, uidxs, oidxs = triplets[:,0], triplets[:,1], triplets[:,2]
            # cut image to improve speed
            ubboxes = ubboxes[:uidxs.max()+1]
            
            if self.training:
                # obtaining all union boxes
                unfold_ubboxes = ubboxes[uidxs]
                unfold_sprobs, unfold_oprobs = bprobs[sidxs], bprobs[oidxs]

                gt_ubboxes = gt_relationships.ubboxes[idx]
                gt_upreds = gt_relationships.upreds[idx]
                gt_umasks = gt_relationships.umasks[idx]

                if len(gt_ubboxes) == 0:
                    continue
                
                # compute gt_smasks and gt_omasks
                gt_smasks = gt_umasks.view(
                    gt_umasks.shape[0], self.num_obj_classes, -1
                ).sum(dim=2)
                gt_omasks = gt_umasks.view(
                    gt_umasks.shape[0], -1, self.num_obj_classes
                ).sum(dim=1)

                # -1 negative, >= 0 positive
                labels = self.match(
                    unfold_ubboxes, unfold_sprobs, unfold_oprobs,
                    gt_ubboxes, gt_smasks, gt_omasks, 
                )
                
                # produce positive
                pos_mask = labels >= 0
                if pos_mask.sum() > 0:
                    pos_unfold_ubboxes = unfold_ubboxes[pos_mask]
                    pos_unfold_upreds = gt_upreds[labels[pos_mask]]

                    pos_ubboxes, inverses = pos_unfold_ubboxes.unique(
                        return_inverse=True, dim=0
                    )
                    pos_upreds = torch.zeros(
                        len(pos_ubboxes), self.num_pred_classes, 
                        device=ubboxes.device
                    )
                    for i, inv in enumerate(inverses):
                        pos_upreds[inv] += pos_unfold_upreds[i]  
                    
                    # add gt during training
                    ubboxes = torch.cat([pos_ubboxes, gt_ubboxes])
                    upreds = torch.cat(
                        [pos_upreds, gt_upreds.float()]
                    ).clamp(0, 1)
                else:
                    ubboxes = gt_ubboxes
                    upreds = gt_upreds.float()

                # produce negatives
                neg_mask = labels < 0
                if neg_mask.sum() > 0:
                    neg_unfold_ubboxes = unfold_ubboxes[neg_mask]
                    neg_ubboxes = neg_unfold_ubboxes.unique(dim=0)

                    #num_neg = self.n2p * len(ubboxes)
                    #neg_ubboxes = neg_ubboxes[:num_neg]
                    neg_preds = torch.zeros(len(neg_ubboxes),
                        self.num_pred_classes, device=neg_ubboxes.device)

                    # add neg during training
                    ubboxes = torch.cat([ubboxes, neg_ubboxes])
                    upreds = torch.cat([upreds, neg_preds])

                uimages = torch.stack(
                    [self.transform(image[:, ymin:ymax, xmin:xmax]) 
                    for ymin, ymax, xmin, xmax in ubboxes]
                )
                ulogits = self.relcls_net(uimages)

                predictions.append(ulogits)
                targets.append(upreds)
            else:
                uimages = torch.stack(
                    [self.transform(image[:, ymin:ymax, xmin:xmax]) 
                    for ymin, ymax, xmin, xmax in ubboxes]
                )
                uprobs = self.relcls_net(uimages).sigmoid_()
                
                umprobs = triplet_matmul(bprobs[sidxs], uprobs[uidxs], bprobs[oidxs])
                
                idxs_50, relations_50 = topk_relations(umprobs, K=50)
                idxs_100, relations_100 = topk_relations(umprobs, K=100)

                sbboxes, ubboxes, obboxes = bboxes[sidxs], ubboxes[uidxs], bboxes[oidxs]

                relationships_50.append(
                    gt_relationships.create_eval(
                        relations=relations_50, 
                        sbboxes=sbboxes[idxs_50], 
                        ubboxes=ubboxes[idxs_50], 
                        obboxes=obboxes[idxs_50],
                        idx=image_idx
                    )
                )

                relationships_100.append(
                    gt_relationships.create_eval(
                        relations=relations_100, 
                        sbboxes=sbboxes[idxs_100], 
                        ubboxes=ubboxes[idxs_100], 
                        obboxes=obboxes[idxs_100],
                        idx=image_idx
                    )
                )
        
        if self.training:
            if len(predictions) > 0:
                predictions = torch.cat(predictions)
                targets = torch.cat(targets)
                predcls_loss = F.binary_cross_entropy_with_logits(
                    predictions, targets) * self.num_pred_classes
            else:
                predcls_loss = fake_loss(self.relcls_net)
            return dict(predcls_loss=predcls_loss)
        
        return relationships_50[0].extend_eval(relationships_50), \
            relationships_100[0].extend_eval(relationships_100)

    def generate(self, bboxes, bcats, bscores):
        sbboxes, sidxs, ubboxes, obboxes, oidxs = pairwise_ubboxes(bboxes)
        bboxes = bboxes[:max(sidxs.max(), oidxs.max())+1]
        scats, ocats = bcats[sidxs], bcats[oidxs]
        return bboxes, sbboxes, scats, sidxs, ubboxes, obboxes, ocats, oidxs

    def attention(self, rfeatures, relations):
        if relations.dim() < 3:
            _relations = []
            for rfeature in rfeatures:
                _relation = torch.cosine_similarity(
                    rfeature.view(1, -1), relations
                ).softmax(dim=0).matmul(relations)
                _relations.append(_relation)
            relations = torch.stack(_relations)
            return torch.cat([rfeatures, relations], dim=1)
        else:
            _relations = []
            for rfeature, relation in zip(rfeatures, relations):
                _relation = torch.cosine_similarity(
                    rfeature.view(1, -1), relation
                ).softmax(dim=0).matmul(relation)
                _relations.append(_relation)
            relations = torch.stack(_relations)
            return torch.cat([rfeatures, relations], dim=1)
            
    def forward_catreldet(self, batch):
        images, _, gt_relationships = batch
        # define task, then mask 
        if self.training:
            rtype = (torch.rand(1) * 2).int().item()
        else:
            rtype = 1
        rmask = self.masks[rtype]
        bert_relations = rmask.view(1,-1,1,1) * self.bert_relations
        #bert_relations = self.bert_relations

        all_bboxes, all_bcats, all_bscores = self.detect(images)

        all_ubboxes, all_gt_upreds = [], []
        all_sbboxes, all_obboxes = [], []
        all_scats, all_ocats = [], []
        all_sidxs, all_oidxs = [], []
        all_uinvs, all_binvs = [], []
        all_unique_bimages, all_unique_uimages = [], []

        for idx, (image, bboxes, bcats, bscores) in \
            enumerate(zip(images, all_bboxes, all_bcats, all_bscores)):
            
            bboxes, sbboxes, scats, sidxs, ubboxes, obboxes, ocats, oidxs = \
                self.generate(bboxes, bcats, bscores)

            if self.training:
                gt_bboxes = gt_relationships.bboxes[idx]
                gt_sbboxes = gt_relationships.sbboxes[idx]
                gt_scats = gt_relationships.scats[idx]
                gt_sidxs = gt_relationships.sidxs[idx]
                gt_ubboxes = gt_relationships.ubboxes[idx]
                gt_upreds = gt_relationships.upreds[idx]
                gt_obboxes = gt_relationships.obboxes[idx]
                gt_ocats = gt_relationships.ocats[idx]
                gt_oidxs = gt_relationships.oidxs[idx]

                if len(gt_ubboxes) == 0:
                    continue
                
                bboxes, sbboxes, scats, sidxs, ubboxes, upreds, obboxes, ocats, oidxs = self.match_and_sample(
                    bboxes, sbboxes, scats, sidxs, ubboxes, obboxes, ocats, oidxs,
                    gt_bboxes, gt_sbboxes, gt_scats, gt_sidxs, gt_ubboxes, gt_upreds, gt_obboxes, gt_ocats, gt_oidxs
                )
                
                assert len(sbboxes) == len(scats) == len(ubboxes) == len(upreds) == len(obboxes) == len(ocats)
                all_gt_upreds.append(upreds)
            
            all_bboxes[idx] = bboxes
            all_scats.append(scats)
            all_ocats.append(ocats)
            all_ubboxes.append(ubboxes)
            all_sbboxes.append(sbboxes)
            all_obboxes.append(obboxes)
            all_sidxs.append(sidxs)
            all_oidxs.append(oidxs)
            
            unique_bboxes, binvs = bboxes.unique(return_inverse=True, dim=0)
            unique_ubboxes, uinvs = ubboxes.unique(return_inverse=True, dim=0)

            unique_bimages = torch.stack(
                [self.transform(image[:, ymin:ymax, xmin:xmax]) 
                for ymin, ymax, xmin, xmax in unique_bboxes]
            )
            unique_uimages = torch.stack(
                [self.transform(image[:, ymin:ymax, xmin:xmax]) 
                for ymin, ymax, xmin, xmax in unique_ubboxes]
            )
            all_unique_bimages.append(unique_bimages)
            all_unique_uimages.append(unique_uimages)
            all_binvs.append(binvs)
            all_uinvs.append(uinvs)
        
        blens = [len(_) for _ in all_unique_bimages]
        ulens = [len(_) for _ in all_unique_uimages]        
        all_unique_bimages = torch.cat(all_unique_bimages)
        all_unique_uimages = torch.cat(all_unique_uimages)

        bfeatures, ufeatures = self.encoder(all_unique_bimages, all_unique_uimages)
        
        # a very import step. unique to unfold
        bfeatures = split(bfeatures, blens)
        ufeatures = split(ufeatures, ulens)
        bfeatures = [b[i] for b, i in zip(bfeatures, all_binvs)]
        ufeatures = [u[i] for u, i in zip(ufeatures, all_uinvs)]

        # transfer to sfeatures and ofeatures
        sfeatures, ofeatures = [], []
        for bfs, sidxs, oidxs in zip(bfeatures, all_sidxs, all_oidxs):
            sfeatures.append(bfs[sidxs])
            ofeatures.append(bfs[oidxs])
        scats = torch.cat(all_scats) 
        ocats = torch.cat(all_ocats)
        sfeatures = torch.cat(sfeatures)
        ufeatures = torch.cat(ufeatures)
        ofeatures = torch.cat(ofeatures)
        
        ufeatures = torch.cat([sfeatures,ufeatures,ofeatures], dim=1)
        ufeatures = self.unet(ufeatures)
        rfeatures = self.attention(ufeatures, bert_relations[scats,:,ocats])
        rfeatures = self.rnet(rfeatures)
        
        ulogits = self.rfc(rfeatures)
        
        if self.training:
            gt_upreds = torch.cat(all_gt_upreds)
            gt_upreds = F.one_hot(gt_upreds.long(), self.num_pred_classes + 1)[:, :self.num_pred_classes]
            gt_upreds = gt_upreds * rmask
            upred_loss = F.binary_cross_entropy_with_logits(ulogits, gt_upreds.float()) * self.num_pred_classes 
            return dict(upred_loss=upred_loss)
        else:
            uprobs = ulogits.sigmoid_()
            uprobs = uprobs * rmask
            ulens = [len(_) for _ in all_ubboxes]
            all_uprobs = split(uprobs, ulens)
            relationships_25, relationships_50, relationships_100 = [], [], []
            for idx, (scats, uprobs, ocats, sbboxes, ubboxes, obboxes) in \
                enumerate(zip(all_scats, all_uprobs, all_ocats, all_sbboxes, all_ubboxes, all_obboxes)):

                assert len(scats) == len(uprobs) == len(ocats) == len(sbboxes) == len(ubboxes) == len(obboxes)
                
                idxs_25, upreds_25 = topk_relations(uprobs, 10)
                idxs_50, upreds_50 = topk_relations(uprobs, 50)
                idxs_100, upreds_100 = topk_relations(uprobs, 100)
                
                relations_25 = torch.stack([scats[idxs_25], upreds_25.squeeze(), ocats[idxs_25]], dim=1)
                relations_50 = torch.stack([scats[idxs_50], upreds_50.squeeze(), ocats[idxs_50]], dim=1)
                relations_100 = torch.stack([scats[idxs_100], upreds_100.squeeze(), ocats[idxs_100]], dim=1)

                relationships_25.append(
                    gt_relationships.create_eval(
                        relations=relations_25, 
                        sbboxes=sbboxes[idxs_25], 
                        ubboxes=ubboxes[idxs_25], 
                        obboxes=obboxes[idxs_25],
                        idx=gt_relationships.idxs[idx]
                    )
                )

                relationships_50.append(
                    gt_relationships.create_eval(
                        relations=relations_50, 
                        sbboxes=sbboxes[idxs_50], 
                        ubboxes=ubboxes[idxs_50], 
                        obboxes=obboxes[idxs_50],
                        idx=gt_relationships.idxs[idx]
                    )
                )

                relationships_100.append(
                    gt_relationships.create_eval(
                        relations=relations_100, 
                        sbboxes=sbboxes[idxs_100], 
                        ubboxes=ubboxes[idxs_100], 
                        obboxes=obboxes[idxs_100],
                        idx=gt_relationships.idxs[idx]
                    )
                )
            return relationships_25[0].extend_eval(relationships_25), \
                relationships_50[0].extend_eval(relationships_50), \
                relationships_100[0].extend_eval(relationships_100)

    def forward(self, batch):
        return getattr(self, "forward_" + self.task.lower())(batch)
        
